"""Bharat x402 — INR facilitator.

This is the new piece. In stock x402 the facilitator verifies an EIP-3009
signature and settles USDC on an EVM chain. This one verifies an HMAC-signed
INR commitment and settles through Razorpay instead.

It speaks the real x402 facilitator contract, so the publisher's resource
server can use the stock `@x402/express` middleware against it unmodified:

    GET  /supported    schemes and networks this facilitator handles
    POST /verify       is this payment proof good?
    POST /settle       make it final

plus two endpoints specific to the INR problem:

    POST /offer        quote an agent a signed price
    POST /settle-batch collapse a day of commitments into one Razorpay charge

---------------------------------------------------------------------------
THE IDEA
---------------------------------------------------------------------------
`/settle` does not touch Razorpay. It records a **commitment** — this agent
owes this publisher ₹5 — and returns immediately. Real money moves later, when
`/settle-batch` turns a few hundred of those commitments into a single Payment
Link.

That indirection is what makes rupee-denominated agent payments work at all.
Razorpay will not process an order below ₹1, and a hosted checkout page cannot
sit in the path of a machine-to-machine HTTP request. Deferring lets a
publisher price a call at ₹0.50 and still get paid. See razorpay_client.py for
the cost model.

It also fits the x402 middleware exactly as-is: the middleware buffers the
handler's response and only releases it once `/settle` succeeds, so an
instant ledger write is a perfectly conformant settlement.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ledger import Ledger, today_utc
from payment_verifier import (
    OfferPolicy,
    VerificationError,
    agent_commitment_body,
    build_offer,
    sign,
    verify_payment_proof,
)
from pydantic import BaseModel, Field
from razorpay_client import (
    FeeModel,
    RazorpayConfigError,
    RazorpayGateway,
    estimate_settlement_cost,
    format_paise,
)

load_dotenv()

PORT = int(os.getenv("PORT", "8402"))

# CAIP-style network id. `razorpay` is the namespace — an off-chain settlement
# rail rather than a blockchain — and `inr-test` the reference.
X402_NETWORK = os.getenv("X402_NETWORK", "razorpay:inr-test")

# Our scheme name. Stock x402 uses "exact" for EVM transfers.
X402_SCHEME = "razorpay-inr"

# Protocol version implemented by @x402/core 2.x.
X402_VERSION = 2

# deferred:    book a commitment now, charge one batch later. The point of this project.
# per_request: hit Razorpay on every single request. Supported so the demo can
#              show it failing on sub-₹1 amounts.
SETTLEMENT_MODE = os.getenv("SETTLEMENT_MODE", "deferred")

HMAC_SECRET = os.getenv("FACILITATOR_HMAC_SECRET", "dev-only-shared-secret-change-me")

OFFER_POLICY = OfferPolicy(
    ttl_seconds=int(os.getenv("OFFER_TTL_SECONDS", "300")),
    scheme=X402_SCHEME,
    network=X402_NETWORK,
    asset="INR",
    max_amount_paise=int(os.getenv("MAX_OFFER_PAISE", "100000")),
)

# The account rupees land in. A real deployment resolves this per publisher
# from the payTo in the payment requirements; the demo has one publisher.
FACILITATOR_ACCOUNT = os.getenv("FACILITATOR_ACCOUNT", "acc_BharatX402TestFacilitator")

ledger = Ledger(os.getenv("LEDGER_DB_PATH", "./data/ledger.db"))
gateway = RazorpayGateway(
    key_id=os.getenv("RAZORPAY_KEY_ID"),
    key_secret=os.getenv("RAZORPAY_KEY_SECRET"),
)
fee_model = FeeModel.from_env()

app = FastAPI(
    title="Bharat x402 Facilitator",
    description="INR settlement facilitator for the x402 agent payment protocol.",
    version="0.3.0",
)


@app.exception_handler(RequestValidationError)
async def malformed_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Records a request this service could not even parse.

    FastAPI's default 422 is fine for the caller but tells the operator
    nothing. A malformed /verify body is a real event — a client integrating
    against the wrong shape, or something probing the endpoint — and it belongs
    in the audit trail alongside the payments that were rejected for better
    reasons.
    """
    ledger.log_event(
        "malformed_request",
        status="rejected",
        path=request.url.path,
        reason="validation_error",
        errors=[
            {"field": ".".join(str(part) for part in err.get("loc", [])), "problem": err.get("msg")}
            for err in exc.errors()[:5]
        ],
    )
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "message": "Request body does not match this endpoint's schema.",
            "detail": exc.errors()[:5],
        },
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence: nothing leaves this service unlogged.

    An unhandled exception in a payment path is the worst kind of silent
    failure — the caller sees a 500 and the operator sees nothing. This records
    what broke and where before answering.

    It deliberately does not leak the exception text to the caller. An error
    from deep in the ledger or the Razorpay SDK can carry connection strings or
    key fragments, and the caller here is an untrusted agent.
    """
    ledger.log_event(
        "unhandled_error",
        status="rejected",
        path=request.url.path,
        reason=type(exc).__name__,
        message=str(exc)[:500],
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "The facilitator failed to process this request. It has been logged.",
        },
    )


@app.on_event("startup")
def announce() -> None:
    """Logs the configuration the facilitator actually came up with.

    Worth being loud about: whether real Razorpay calls will happen is the one
    thing an operator most needs to know at a glance.
    """
    ledger.log_event(
        "facilitator_started",
        status="ok",
        settlementMode=SETTLEMENT_MODE,
        razorpayMode=gateway.mode,
        network=X402_NETWORK,
        scheme=X402_SCHEME,
        offerTtlSeconds=OFFER_POLICY.ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class OfferRequest(BaseModel):
    """An agent asking to be quoted for a resource."""

    agentId: str = Field(..., description="Identity of the paying agent.")
    resourceId: str = Field(..., description="What is being bought.")
    amountPaise: int = Field(..., description="Price in paise, from the 402 `accepts` entry.")
    payTo: str = Field(..., description="Publisher's settlement account, from the 402.")
    asset: str = Field(default="INR")
    scheme: str = Field(default=X402_SCHEME)
    network: str = Field(default=X402_NETWORK)
    resourceUrl: str | None = Field(default=None)


class X402Request(BaseModel):
    """The body `HTTPFacilitatorClient` posts to /verify and /settle."""

    x402Version: int
    paymentPayload: dict[str, Any]
    paymentRequirements: dict[str, Any]


class BatchRequest(BaseModel):
    """A request to settle outstanding commitments."""

    agentId: str | None = Field(
        default=None, description="Settle one agent only. Omit to settle every agent that owes."
    )
    settleDate: str | None = Field(
        default=None, description="YYYY-MM-DD. Defaults to today (UTC)."
    )
    dryRun: bool = Field(
        default=False, description="Compute the batch and the fee comparison without charging."
    )


# ---------------------------------------------------------------------------
# x402 facilitator contract
# ---------------------------------------------------------------------------


@app.get("/supported")
def supported() -> dict:
    """Advertise what this facilitator can settle.

    The standard x402 discovery endpoint. The resource server calls it at
    startup and refuses to price a route whose scheme/network we do not claim
    here — the mechanism that makes x402 facilitator-agnostic in practice
    rather than only on paper.

    `extra` is passed through into the 402 body, so agents learn the settlement
    mode from the facilitator actually running rather than from something
    hardcoded in the publisher's config.
    """
    return {
        "kinds": [
            {
                "x402Version": X402_VERSION,
                "scheme": X402_SCHEME,
                "network": X402_NETWORK,
                "extra": {
                    "currency": "INR",
                    "decimals": 2,  # amounts travel as integer paise
                    "settlementRail": "razorpay",
                    "settlementMode": SETTLEMENT_MODE,
                    "razorpayMode": gateway.mode,
                    # Named honestly: this is not the EVM signature scheme.
                    "proofScheme": "hmac-sha256",
                    "offerEndpoint": "/offer",
                    "minimumChargePaise": fee_model.minimum_charge_paise,
                },
            }
        ],
        # No x402 protocol extensions (bazaar, sign-in-with-x, ...) implemented.
        "extensions": [],
        # For EVM facilitators this maps network family to signer addresses.
        # Our equivalent is the merchant account rupees land in.
        "signers": {"razorpay:*": [FACILITATOR_ACCOUNT]},
    }


@app.post("/verify")
def verify(request: X402Request) -> JSONResponse:
    """Check a payment proof without spending it.

    Deliberately side-effect free apart from the audit log. The x402 middleware
    calls /verify *before* running the publisher's handler and /settle after,
    so verification has to be safe to call speculatively and repeatedly — it
    must not consume the offer.

    Returns:
        The x402 `VerifyResponse` shape. Failures are 200s with
        `isValid: false`, because a rejected payment is a normal protocol
        outcome, not a transport error.
    """
    payload = request.paymentPayload.get("payload", {})
    requirements = request.paymentRequirements
    offer_id = payload.get("offerId")

    try:
        offer_row = ledger.get_offer(offer_id) if offer_id else None
        result = verify_payment_proof(
            payload=payload,
            offer_row=offer_row,
            requirements=requirements,
            secret=HMAC_SECRET,
        )
    except VerificationError as exc:
        # Every rejection is recorded with its reason. "The payment failed" is
        # not a useful thing to find in a log a week later.
        ledger.log_event(
            "payment_verify_rejected",
            agent_id=payload.get("agentId"),
            amount_paise=int(requirements.get("amount", 0) or 0),
            status="rejected",
            reason=exc.reason,
            message=exc.message,
            offerId=offer_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "isValid": False,
                "invalidReason": exc.reason,
                "invalidMessage": exc.message,
            },
        )

    ledger.log_event(
        "payment_verified",
        agent_id=result["agentId"],
        resource_id=result["resourceId"],
        amount_paise=result["amountPaise"],
        status="ok",
        offerId=result["offerId"],
    )

    return JSONResponse(
        status_code=200,
        content={
            "isValid": True,
            "payer": result["agentId"],
            "extra": {
                "offerId": result["offerId"],
                "settlementMode": SETTLEMENT_MODE,
                "humanAmount": format_paise(result["amountPaise"]),
            },
        },
    )


@app.post("/settle")
def settle(request: X402Request) -> JSONResponse:
    """Make a verified payment final.

    In `deferred` mode — the interesting one — this books a commitment against
    the ledger and returns. No Razorpay call happens here. The `transaction` id
    the protocol wants is our commitment id: a real, auditable reference to a
    debt that will be charged in the next batch.

    In `per_request` mode it creates a Payment Link immediately, which is
    included mainly so the demo can show it refusing to charge sub-₹1 amounts.

    Idempotent on the offer: a retried settlement returns the original
    commitment rather than creating a second debt. Retries happen — networks
    fail mid-response — and charging twice for one fetch is the worst thing a
    payment system can do.
    """
    payload = request.paymentPayload.get("payload", {})
    requirements = request.paymentRequirements
    offer_id = payload.get("offerId")

    def failure(reason: str, message: str, agent_id: str | None = None) -> JSONResponse:
        ledger.log_event(
            "payment_settle_rejected",
            agent_id=agent_id or payload.get("agentId"),
            amount_paise=int(requirements.get("amount", 0) or 0),
            status="rejected",
            reason=reason,
            message=message,
            offerId=offer_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "errorReason": reason,
                "errorMessage": message,
                "transaction": "",
                "network": X402_NETWORK,
            },
        )

    # Re-verify rather than trusting that /verify ran. The two calls are
    # separate HTTP requests and a facilitator must not assume ordering.
    try:
        offer_row = ledger.get_offer(offer_id) if offer_id else None
        result = verify_payment_proof(
            payload=payload,
            offer_row=offer_row,
            requirements=requirements,
            secret=HMAC_SECRET,
        )
    except VerificationError as exc:
        return failure(exc.reason, exc.message)

    # Idempotency: this offer may already have produced a commitment.
    existing = ledger.get_commitment_by_offer(result["offerId"])
    if existing is not None:
        ledger.log_event(
            "settlement_replayed",
            agent_id=existing["agent_id"],
            resource_id=existing["resource_id"],
            amount_paise=existing["amount_paise"],
            status="ok",
            commitmentId=existing["commitment_id"],
            note="Returning the existing commitment rather than double-charging.",
        )
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "transaction": existing["commitment_id"],
                "network": X402_NETWORK,
                "payer": existing["agent_id"],
                "amount": str(existing["amount_paise"]),
                "extra": {"replayed": True, "settlementMode": existing["mode"]},
            },
        )

    commitment_id = f"cmt_{uuid.uuid4().hex[:20]}"

    try:
        commitment = ledger.create_commitment(
            commitment_id=commitment_id,
            offer_id=result["offerId"],
            agent_id=result["agentId"],
            resource_id=result["resourceId"],
            amount_paise=result["amountPaise"],
            asset=result["asset"],
            mode=SETTLEMENT_MODE,
        )
    except ValueError as exc:
        # Lost a race against a concurrent settle, or the offer was already
        # spent. Either way the correct answer is the existing commitment.
        existing = ledger.get_commitment_by_offer(result["offerId"])
        if existing is not None:
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "transaction": existing["commitment_id"],
                    "network": X402_NETWORK,
                    "payer": existing["agent_id"],
                    "extra": {"replayed": True},
                },
            )
        return failure("offer_unavailable", str(exc), agent_id=result["agentId"])

    if SETTLEMENT_MODE == "per_request":
        return _settle_immediately(commitment)

    ledger.log_event(
        "commitment_recorded",
        agent_id=commitment["agentId"],
        resource_id=commitment["resourceId"],
        amount_paise=commitment["amountPaise"],
        status="pending",
        commitmentId=commitment["commitmentId"],
        settleDate=commitment["settleDate"],
        note="Deferred — no Razorpay call. Charged in the next batch.",
    )

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            # The protocol wants a settlement reference. Ours is a commitment
            # id, not a chain transaction hash — an honest identifier for what
            # actually happened.
            "transaction": commitment["commitmentId"],
            "network": X402_NETWORK,
            "payer": commitment["agentId"],
            "amount": str(commitment["amountPaise"]),
            "extra": {
                "settlementMode": "deferred",
                "settleDate": commitment["settleDate"],
                "humanAmount": format_paise(commitment["amountPaise"]),
                "note": "Commitment recorded; rupees move in the next batched Payment Link.",
            },
        },
    )


def _settle_immediately(commitment: dict) -> JSONResponse:
    """Charge one commitment on its own, the naive way.

    Kept so the comparison is demonstrable rather than asserted: run the demo
    with SETTLEMENT_MODE=per_request and a sub-₹1 price and watch the gateway
    refuse it.
    """
    batch_id = f"batch_{uuid.uuid4().hex[:16]}"
    try:
        link = gateway.create_payment_link(
            amount_paise=commitment["amountPaise"],
            description=f"x402 request: {commitment['resourceId']}",
            reference_id=batch_id,
            agent_id=commitment["agentId"],
            notes={"commitment_id": commitment["commitmentId"], "mode": "per_request"},
        )
    except (RazorpayConfigError, Exception) as exc:  # noqa: BLE001 - reported, never swallowed
        ledger.record_batch(
            batch_id=batch_id,
            agent_id=commitment["agentId"],
            settle_date=commitment["settleDate"],
            commitment_ids=[commitment["commitmentId"]],
            total_paise=commitment["amountPaise"],
            payment_link_id=None,
            payment_link_url=None,
            status="failed",
            razorpay_mode=gateway.mode,
            error_message=str(exc),
        )
        ledger.log_event(
            "per_request_settlement_failed",
            agent_id=commitment["agentId"],
            amount_paise=commitment["amountPaise"],
            status="rejected",
            reason=type(exc).__name__,
            message=str(exc),
        )
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "errorReason": "settlement_failed",
                "errorMessage": str(exc),
                "transaction": "",
                "network": X402_NETWORK,
            },
        )

    ledger.record_batch(
        batch_id=batch_id,
        agent_id=commitment["agentId"],
        settle_date=commitment["settleDate"],
        commitment_ids=[commitment["commitmentId"]],
        total_paise=commitment["amountPaise"],
        payment_link_id=link["id"],
        payment_link_url=link.get("short_url"),
        status="created",
        razorpay_mode=gateway.mode,
    )
    ledger.log_event(
        "per_request_settled",
        agent_id=commitment["agentId"],
        amount_paise=commitment["amountPaise"],
        status="ok",
        paymentLinkId=link["id"],
    )

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "transaction": link["id"],
            "network": X402_NETWORK,
            "payer": commitment["agentId"],
            "amount": str(commitment["amountPaise"]),
            "extra": {"settlementMode": "per_request", "paymentLinkUrl": link.get("short_url")},
        },
    )


# ---------------------------------------------------------------------------
# INR-specific endpoints
# ---------------------------------------------------------------------------


@app.post("/offer")
def offer(request: OfferRequest) -> JSONResponse:
    """Quote an agent a signed, time-limited price.

    Stock x402 has no equivalent: an EVM agent already holds a wallet and can
    construct a signed transfer authorisation unaided. An agent paying in
    rupees has no such instrument, so it asks the facilitator to quote it and
    signs its acceptance of that quote instead.

    The offer is bound to one agent, one resource, and one amount, expires, and
    can be spent exactly once.

    Returns:
        `{"offer": {...}, "signature": "...", "commitmentTemplate": {...}}` —
        the template being the exact object the agent must sign, so a client
        implementer never has to guess at field order or naming.
    """
    if request.scheme != X402_SCHEME or request.network != X402_NETWORK:
        ledger.log_event(
            "offer_rejected",
            agent_id=request.agentId,
            resource_id=request.resourceId,
            status="rejected",
            reason="unsupported_kind",
            requested=f"{request.scheme}@{request.network}",
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_kind",
                "message": (
                    f"This facilitator settles {X402_SCHEME} on {X402_NETWORK}, "
                    f"not {request.scheme} on {request.network}."
                ),
            },
        )

    try:
        offer_body = build_offer(
            agent_id=request.agentId,
            resource_id=request.resourceId,
            amount_paise=request.amountPaise,
            pay_to=request.payTo,
            policy=OFFER_POLICY,
            resource_url=request.resourceUrl,
        )
    except VerificationError as exc:
        ledger.log_event(
            "offer_rejected",
            agent_id=request.agentId,
            resource_id=request.resourceId,
            amount_paise=request.amountPaise,
            status="rejected",
            reason=exc.reason,
            message=exc.message,
        )
        return JSONResponse(
            status_code=400, content={"error": exc.reason, "message": exc.message}
        )

    signature = sign(offer_body, HMAC_SECRET)
    ledger.insert_offer(offer_body, signature)

    ledger.log_event(
        "offer_issued",
        agent_id=request.agentId,
        resource_id=request.resourceId,
        amount_paise=offer_body["amountPaise"],
        status="ok",
        offerId=offer_body["offerId"],
        expiresAt=offer_body["expiresAt"],
    )

    return JSONResponse(
        status_code=200,
        content={
            "offer": offer_body,
            "signature": signature,
            "humanAmount": format_paise(offer_body["amountPaise"]),
            # Hand the client the exact bytes to sign. Removes the single most
            # error-prone step in implementing against a signature scheme.
            "commitmentTemplate": agent_commitment_body(offer_body, "<acceptedAt>"),
            "instructions": (
                "Sign commitmentTemplate (with acceptedAt set to an ISO-8601 UTC timestamp) "
                "using HMAC-SHA256 over its canonical JSON — sorted keys, no whitespace. "
                "Send the result as the x402 payment payload."
            ),
        },
    )


@app.post("/settle-batch")
def settle_batch(request: BatchRequest) -> JSONResponse:
    """Collapse outstanding commitments into one Razorpay Payment Link per agent.

    This is the endpoint the whole project exists to demonstrate. A few hundred
    ₹5 debts, accumulated over a day with no gateway involvement at all, become
    one charge.

    Batching is per agent, not global: each agent is a distinct counterparty
    and gets its own link. In production a single agent operator would more
    likely hold a UPI Autopay mandate and be debited directly.

    Args:
        request: Optional agent/date filters and a dry-run switch.

    Returns:
        Per-agent batch results plus the settlement cost comparison.
    """
    settle_date = request.settleDate or today_utc()
    agents = (
        [request.agentId]
        if request.agentId
        else ledger.pending_agents(settle_date=settle_date)
    )

    if not agents:
        ledger.log_event(
            "batch_noop", status="ok", settleDate=settle_date, note="Nothing outstanding."
        )
        return JSONResponse(
            status_code=200,
            content={
                "settleDate": settle_date,
                "batches": [],
                "message": f"No pending commitments for {settle_date}.",
            },
        )

    batches = []
    all_amounts: list[int] = []

    for agent_id in agents:
        pending = ledger.pending_commitments(agent_id=agent_id, settle_date=settle_date)
        if not pending:
            continue

        amounts = [int(row["amount_paise"]) for row in pending]
        commitment_ids = [row["commitment_id"] for row in pending]
        total = sum(amounts)
        all_amounts.extend(amounts)

        economics = estimate_settlement_cost(amounts, fee_model)
        batch_id = f"batch_{uuid.uuid4().hex[:16]}"

        if request.dryRun:
            ledger.log_event(
                "batch_dry_run",
                agent_id=agent_id,
                amount_paise=total,
                status="ok",
                commitmentCount=len(commitment_ids),
                settleDate=settle_date,
            )
            # Same keys as a real batch, with the gateway fields null. A client
            # should not have to branch on `status` just to read a response.
            batches.append(
                {
                    "batchId": None,
                    "agentId": agent_id,
                    "settleDate": settle_date,
                    "commitmentCount": len(commitment_ids),
                    "totalPaise": total,
                    "humanTotal": format_paise(total),
                    "status": "dry_run",
                    "paymentLinkId": None,
                    "paymentLinkUrl": None,
                    "razorpayMode": gateway.mode,
                    "economics": economics,
                }
            )
            continue

        description = (
            f"Bharat x402 — {len(commitment_ids)} agent requests on {settle_date} "
            f"({format_paise(total)})"
        )

        try:
            link = gateway.create_payment_link(
                amount_paise=total,
                description=description,
                reference_id=batch_id,
                agent_id=agent_id,
                notes={
                    "settle_date": settle_date,
                    "commitments": str(len(commitment_ids)),
                },
            )
        except Exception as exc:  # noqa: BLE001 - recorded and reported, never swallowed
            ledger.record_batch(
                batch_id=batch_id,
                agent_id=agent_id,
                settle_date=settle_date,
                commitment_ids=commitment_ids,
                total_paise=total,
                payment_link_id=None,
                payment_link_url=None,
                status="failed",
                razorpay_mode=gateway.mode,
                error_message=str(exc),
            )
            ledger.log_event(
                "batch_failed",
                agent_id=agent_id,
                amount_paise=total,
                status="rejected",
                reason=type(exc).__name__,
                message=str(exc),
                note="Commitments stay pending and will be retried next run.",
            )
            batches.append(
                {
                    "batchId": batch_id,
                    "agentId": agent_id,
                    "settleDate": settle_date,
                    "commitmentCount": len(commitment_ids),
                    "totalPaise": total,
                    "humanTotal": format_paise(total),
                    "status": "failed",
                    "paymentLinkId": None,
                    "paymentLinkUrl": None,
                    "razorpayMode": gateway.mode,
                    "error": str(exc),
                    "economics": economics,
                }
            )
            continue

        ledger.record_batch(
            batch_id=batch_id,
            agent_id=agent_id,
            settle_date=settle_date,
            commitment_ids=commitment_ids,
            total_paise=total,
            payment_link_id=link["id"],
            payment_link_url=link.get("short_url"),
            status="created",
            razorpay_mode=gateway.mode,
        )
        ledger.log_event(
            "batch_settled",
            agent_id=agent_id,
            amount_paise=total,
            status="ok",
            batchId=batch_id,
            commitmentCount=len(commitment_ids),
            paymentLinkId=link["id"],
            paymentLinkUrl=link.get("short_url"),
            razorpayMode=gateway.mode,
        )

        batches.append(
            {
                "batchId": batch_id,
                "agentId": agent_id,
                "settleDate": settle_date,
                "commitmentCount": len(commitment_ids),
                "totalPaise": total,
                "humanTotal": format_paise(total),
                "status": "created",
                "paymentLinkId": link["id"],
                "paymentLinkUrl": link.get("short_url"),
                "razorpayMode": link["mode"],
                "economics": economics,
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "settleDate": settle_date,
            "dryRun": request.dryRun,
            "batchCount": len(batches),
            "batches": batches,
            "aggregate": estimate_settlement_cost(all_amounts, fee_model) if all_amounts else None,
        },
    )


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


@app.get("/ledger/summary")
def ledger_summary(settleDate: str | None = None) -> dict:
    """One day's activity, for the reporting script and for eyeballing a demo."""
    return ledger.daily_summary(settleDate)


@app.get("/health")
def health() -> dict:
    return {
        "service": "facilitator",
        "status": "ok",
        "settlementMode": SETTLEMENT_MODE,
        "razorpayMode": gateway.mode,
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "Bharat x402 INR facilitator",
        "scheme": X402_SCHEME,
        "network": X402_NETWORK,
        "settlementMode": SETTLEMENT_MODE,
        "razorpayMode": gateway.mode,
        "x402Endpoints": ["/supported", "/verify", "/settle"],
        "inrEndpoints": ["/offer", "/settle-batch"],
        "operational": ["/health", "/ledger/summary"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
