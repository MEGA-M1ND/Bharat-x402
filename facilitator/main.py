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
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from ledger import DailyCapExceeded, Ledger, redact_credentials, today_utc
from limits import LimitExceeded, SpendPolicy
from payment_verifier import (
    ALGORITHM,
    KeyFormatError,
    OfferPolicy,
    VerificationError,
    agent_commitment_body,
    build_offer,
    load_public_key,
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

# Anchor to this file rather than the working directory. `uvicorn main:app
# --app-dir facilitator` runs from the repo root, and a facilitator that reads
# its config — or worse, writes its ledger — somewhere different depending on
# where you launched it from is a genuinely nasty way to lose a day's revenue.
SERVICE_DIR = Path(__file__).resolve().parent

load_dotenv(SERVICE_DIR / ".env")

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

# Whether an agent with no registered Ed25519 key may still pay with a
# shared-secret HMAC proof. True by default so existing clients — the CLI
# agent's older invocations, anything written against the previous protocol —
# keep working while keys roll out. Set false to require registration, which
# is the end state: see payment_verifier.py on why a shared secret cannot
# provide non-repudiation no matter how strong the MAC is.
ALLOW_HMAC_FALLBACK = os.getenv("ALLOW_HMAC_FALLBACK", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Razorpay's webhook signing secret, from the dashboard's webhook settings.
# Unset means the webhook endpoint refuses every delivery rather than
# accepting unauthenticated ledger writes — see webhooks.py.
RAZORPAY_WEBHOOK_SECRET = (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()

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

# Whether a bare POST /settle-batch (no agentId) is allowed to sweep every
# agent that owes money. True by default so nothing about local dev,
# scheduler.py, or the existing test suite changes. A public multi-visitor
# deployment must set this to false — otherwise one visitor's settle button
# charges every other visitor's pending commitments into their batch.
ALLOW_GLOBAL_SETTLE = os.getenv("ALLOW_GLOBAL_SETTLE", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# The console's server-side agent runner (facilitator/demo_trace.py). Off by
# default: it is a public write endpoint that creates ledger rows on request,
# which is fine for a demo deployment and not something to expose silently.
ENABLE_DEMO_API = os.getenv("ENABLE_DEMO_API", "false").strip().lower() in ("1", "true", "yes")

# Where the publisher's resource server lives — only used by the demo API, to
# make the real cross-service HTTP calls a browser client can't safely make
# itself (see demo_trace.py for why). A service binding would be the
# lower-latency choice here, but Vercel rejects a binding in this direction:
# the resource server already binds to the facilitator for its own /verify
# and /settle calls, and a facilitator->resource binding on top of that forms
# a circular binding, which Vercel's services model refuses to deploy. So the
# demo agent instead calls the resource server's real public URL — which,
# for a demo whose whole point is showing an agent's actual HTTP traffic, is
# arguably more honest anyway. Derived from VERCEL_PROJECT_PRODUCTION_URL
# rather than VERCEL_URL for the same reason server.js computes its
# FACILITATOR_PUBLIC_URL that way: VERCEL_URL points at the current
# deployment, which sits behind Deployment Protection on a preview.
RESOURCE_URL = os.getenv("RESOURCE_URL") or (
    f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}"
    if os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
    else "http://localhost:3402"
)

# LEDGER_DSN (a postgres:// URL) takes priority when set — that is the
# production path. LEDGER_DB_PATH is the SQLite file path this project has
# always used, kept as the fallback so local dev and every existing .env
# needs no change. Ledger itself decides which engine a string means; see
# db.dialect_for.
ledger = Ledger(
    os.getenv("LEDGER_DSN")
    or os.getenv("LEDGER_DB_PATH")
    or str(SERVICE_DIR / "data" / "ledger.db")
)
gateway = RazorpayGateway(
    key_id=os.getenv("RAZORPAY_KEY_ID"),
    key_secret=os.getenv("RAZORPAY_KEY_SECRET"),
)
fee_model = FeeModel.from_env()

# Per-agent spending controls. `OFFER_POLICY.max_amount_paise` caps one quote;
# this caps what an agent can do with many of them. See limits.py on why a
# transaction-size limit is not a spending limit.
SPEND_POLICY = SpendPolicy.from_env()

app = FastAPI(
    title="Bharat x402 Facilitator",
    description="INR settlement facilitator for the x402 agent payment protocol.",
    version="0.3.0",
)

# CORS. Same-origin once this and the resource server share a deployment, but
# needed for local dev (console on :3402, facilitator on :8402) and for any
# other client poking the API directly. `expose_headers` is the part that is
# easy to miss and silently breaks a browser client — without it, reading
# these headers from a fetch() Response returns null even on a 200, because
# the fetch spec hides non-simple response headers from JS unless the server
# explicitly exposes them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"],
)

import webhooks  # noqa: E402 - imported here, after `ledger` exists to configure it with

webhooks.configure(ledger=ledger, webhook_secret=RAZORPAY_WEBHOOK_SECRET)
app.include_router(webhooks.router)

if ENABLE_DEMO_API:
    import demo_trace

    demo_trace.configure(
        ledger=ledger,
        hmac_secret=HMAC_SECRET,
        offer_policy=OFFER_POLICY,
        resource_url=RESOURCE_URL,
        spend_policy=SPEND_POLICY,
    )
    app.include_router(demo_trace.router)


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


# Whether the startup hook below runs at all. On by default — local dev, CI,
# and the test suite all want to know immediately if Razorpay credentials are
# bad, per the whole point of this hook (see docs/demo-script.md). A
# serverless cold start pays for it on every single invocation instead of
# once per process lifetime, though: two Postgres log writes and, in real
# Razorpay mode, one outbound API call, on every cold start. Vercel sets this
# to "0" — the deployed demo runs in MOCK_RAZORPAY anyway, where
# check_credentials() is a constant with no network call, so there is nothing
# useful for the hook to catch there regardless.
FACILITATOR_STARTUP_CHECK = os.getenv("FACILITATOR_STARTUP_CHECK", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

if FACILITATOR_STARTUP_CHECK:

    @app.on_event("startup")
    def announce() -> None:
        """Logs the configuration the facilitator actually came up with.

        Worth being loud about: whether real Razorpay calls will happen is the
        one thing an operator most needs to know at a glance.
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

        # Find out now whether settlement will actually work, rather than at
        # the end of the first day with a ledger full of commitments behind it.
        ok, detail = gateway.check_credentials()
        ledger.log_event(
            "razorpay_credentials_ok" if ok else "razorpay_credentials_invalid",
            status="ok" if ok else "rejected",
            razorpayMode=gateway.mode,
            message=detail,
            note=None
            if ok
            else (
                "Payments will still be verified and committed to the ledger. "
                "Only /settle-batch is affected; those commitments stay pending."
            ),
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


class AgentRegistration(BaseModel):
    """An agent announcing the public half of its signing key."""

    agentId: str = Field(..., min_length=1, max_length=64)
    publicKey: str = Field(..., description="Base64 of the raw 32-byte Ed25519 public key.")
    algorithm: str = Field(default=ALGORITHM)


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
                    # Named honestly: this is not the EVM signature scheme,
                    # but it is now a real asymmetric one — the agent signs
                    # with a key the facilitator does not hold.
                    "proofScheme": ALGORITHM,
                    "registrationEndpoint": "/agents/register",
                    # Advertised so a client knows whether it *must* register
                    # before paying, rather than discovering it at settlement.
                    "hmacFallbackAllowed": ALLOW_HMAC_FALLBACK,
                    "offerEndpoint": "/offer",
                    "minimumChargePaise": fee_model.minimum_charge_paise,
                    # A limit a client cannot discover is one it can only find
                    # out about by being refused.
                    "maxOfferPaise": OFFER_POLICY.max_amount_paise,
                    "limits": SPEND_POLICY.describe(),
                },
            }
        ],
        # No x402 protocol extensions (bazaar, sign-in-with-x, ...) implemented.
        "extensions": [],
        # For EVM facilitators this maps network family to signer addresses.
        # Our equivalent is the merchant account rupees land in.
        "signers": {"razorpay:*": [FACILITATOR_ACCOUNT]},
    }


def _agent_key_for(offer_row: Any | None) -> Any | None:
    """The registered signing key of the agent an offer was issued to.

    Looks the agent up by `offer_row["agent_id"]` — the id the *facilitator*
    bound the offer to when it quoted — and never by the `agentId` in the
    caller-supplied payload.

    That distinction is load-bearing. Keying off the payload would let an
    attacker holding a registered agent's offer present it under some
    unregistered id, find no key on file, and be dropped onto the HMAC
    fallback path. Reading the id from our own record means the choice of
    which key must verify is not something the caller can influence.
    """
    if offer_row is None:
        return None
    return ledger.get_agent(offer_row["agent_id"])


@app.post("/agents/register")
def register_agent(request: AgentRegistration) -> JSONResponse:
    """Registers an agent's Ed25519 public key.

    The step that replaces "everyone shares one secret". After this, the
    facilitator can verify that agent's commitments and cannot produce them,
    which is what makes a commitment usable as evidence in a dispute rather
    than merely as a checksum.

    First registration wins. Re-sending the same key is a no-op so a restarted
    agent need not track whether it has registered before; sending a
    *different* key for an existing id is refused, because silently accepting
    it would make key rotation and account takeover the same request. Rotation
    for real needs an authenticated channel, which this demo does not have —
    see `Ledger.register_agent` on the trust-on-first-use limitation.

    Returns:
        The stored registration, or 400/409 on a bad or conflicting key.
    """
    if request.algorithm != ALGORITHM:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_algorithm",
                "message": f"This facilitator verifies {ALGORITHM} signatures only.",
            },
        )

    # Parse before storing. A key that cannot be loaded would otherwise be
    # accepted here and only fail later, at settlement time, as a mysterious
    # invalid_signature on a proof that was actually fine.
    try:
        load_public_key(request.publicKey)
    except KeyFormatError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_public_key", "message": str(exc)},
        )

    try:
        registration = ledger.register_agent(
            agent_id=request.agentId,
            public_key=request.publicKey,
            algorithm=request.algorithm,
        )
    except ValueError as exc:
        ledger.log_event(
            "agent_key_conflict",
            agent_id=request.agentId,
            status="rejected",
            reason="already_registered",
            message=str(exc),
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": "agent_already_registered",
                "message": (
                    f"{exc}. Key rotation needs an authenticated channel this demo does "
                    "not implement — deliberately, rather than by allowing a rebind that "
                    "would let anyone take over an agent id."
                ),
            },
        )

    ledger.log_event(
        "agent_registered" if registration["created"] else "agent_registration_replayed",
        agent_id=request.agentId,
        status="ok",
        algorithm=request.algorithm,
        # The public key is safe to log — that is the whole point of it.
        publicKey=request.publicKey,
    )

    return JSONResponse(status_code=200, content=registration)


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> JSONResponse:
    """Returns an agent's registered public key, if it has one.

    Public on purpose. A public key is not a secret, and exposing it lets a
    publisher — or an auditor settling a dispute — independently verify a
    commitment against the key on file without asking the facilitator to
    mark its own homework.
    """
    row = ledger.get_agent(agent_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "agent_not_registered",
                "message": f"No signing key registered for {agent_id}.",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "agentId": row["agent_id"],
            "publicKey": row["public_key"],
            "algorithm": row["algorithm"],
            "registeredAt": row["registered_at"],
        },
    )


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
            agent_key=_agent_key_for(offer_row),
            allow_hmac_fallback=ALLOW_HMAC_FALLBACK,
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
        proofScheme=result["proofScheme"],
        # Surfaced on every fallback verification rather than only at rollout,
        # so a migration that quietly never finishes is visible in the audit
        # trail instead of being mistaken for a completed one.
        downgraded=result["proofScheme"] != ALGORITHM,
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
                "proofScheme": result["proofScheme"],
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
            agent_key=_agent_key_for(offer_row),
            allow_hmac_fallback=ALLOW_HMAC_FALLBACK,
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
            daily_cap_paise=SPEND_POLICY.cap_for_sql,
        )
    except DailyCapExceeded as exc:
        # The binding check, and the one that holds under concurrency — the
        # quote-time check above can be raced by two settlements arriving at
        # once. The offer is left spendable: the transaction rolled back, so a
        # cap breach costs the agent nothing but this request.
        ledger.log_event(
            "settle_refused_by_policy",
            agent_id=exc.agent_id,
            resource_id=result["resourceId"],
            amount_paise=exc.amount_paise,
            status="rejected",
            reason="daily_cap_exceeded",
            message=str(exc),
            dailyCapPaise=exc.daily_cap_paise,
            committedPaise=exc.committed_paise,
            remainingPaise=exc.remaining_paise,
            note="Offer left open — the transaction rolled back.",
        )
        return failure("daily_cap_exceeded", str(exc), agent_id=exc.agent_id)
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

    # Spending controls, before anything is written. Refusing at quote time is
    # the useful place: the agent finds out before it signs, and a refused
    # quote costs the facilitator one row it does not create.
    try:
        SPEND_POLICY.check_admission(request.agentId)
        SPEND_POLICY.check_offer_rate(
            request.agentId,
            ledger.recent_offer_count(agent_id=request.agentId),
        )
        SPEND_POLICY.check_daily_cap(
            request.agentId,
            ledger.committed_today(agent_id=request.agentId),
            request.amountPaise,
        )
    except LimitExceeded as exc:
        ledger.log_event(
            "offer_refused_by_policy",
            agent_id=request.agentId,
            resource_id=request.resourceId,
            amount_paise=request.amountPaise,
            status="rejected",
            reason=exc.reason,
            message=exc.message,
            **exc.detail,
        )
        return JSONResponse(
            status_code=429 if exc.reason == "offer_rate_exceeded" else 403,
            content={"error": exc.reason, "message": exc.message, **exc.detail},
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
    if not request.agentId and not ALLOW_GLOBAL_SETTLE:
        return JSONResponse(
            status_code=400,
            content={
                "error": "agent_id_required",
                "message": (
                    "This facilitator requires an agentId for /settle-batch. Settling "
                    "without one would sweep every visitor's pending commitments into "
                    "a single run."
                ),
            },
        )

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
def ledger_summary(settleDate: str | None = None, agentId: str | None = None) -> dict:
    """One day's activity, for the reporting script and for eyeballing a demo.

    `agentId` narrows to one agent's traffic — what the console's dashboard
    uses so one visitor's session does not show, or via `/settle-batch`,
    settle, everyone else's pending commitments too.
    """
    return ledger.daily_summary(settleDate, agent_id=agentId)


@app.get("/agents/{agent_id}/limits")
def agent_limits(agent_id: str, settleDate: str | None = None) -> dict:
    """What this agent has spent against its cap, and what is left.

    Exists so an agent can manage its own budget instead of discovering the
    ceiling by being refused mid-task. Safe to expose: it reports one agent's
    own totals, which that agent already knows, and the policy figures are
    advertised on `/supported` regardless.
    """
    settle_date = settleDate or today_utc()
    committed = ledger.committed_today(agent_id=agent_id, settle_date=settle_date)
    cap = SPEND_POLICY.daily_cap_paise

    return {
        "agentId": agent_id,
        "settleDate": settle_date,
        "committedPaise": committed,
        "dailyCapPaise": cap or None,
        "remainingPaise": max(cap - committed, 0) if cap > 0 else None,
        "recentOffersLastMinute": ledger.recent_offer_count(agent_id=agent_id),
        "offerRatePerMinute": SPEND_POLICY.offer_rate_per_minute or None,
        "frozen": agent_id in SPEND_POLICY.frozen_agents,
        "acceptingPayments": SPEND_POLICY.accept_payments,
    }


@app.get("/economics")
def economics(settleDate: str | None = None, agentId: str | None = None) -> dict:
    """The batching-versus-per-request comparison, as a read.

    `/settle-batch` computes the same figures but only ever sees commitments
    still `pending` — so a card built the same way would go blank the instant
    a batch clears. This looks at every commitment for the day regardless of
    status, so the number stays put after settling.
    """
    settle_date = settleDate or today_utc()
    amounts = ledger.commitment_amounts(agent_id=agentId, settle_date=settle_date)
    return {
        "settleDate": settle_date,
        "agentId": agentId,
        "economics": estimate_settlement_cost(amounts, fee_model) if amounts else None,
        # Charges grouped by size, so a client can draw the gateway floor
        # rather than being told about it. Same numbers as `economics`, shaped
        # for a chart instead of a paragraph.
        "distribution": ledger.commitment_histogram(
            agent_id=agentId, settle_date=settle_date
        ),
        "gatewayMinimumPaise": fee_model.minimum_charge_paise,
    }


@app.get("/ledger/events")
def ledger_events(
    agentId: str | None = None, limit: int = 50, sinceId: int | None = None
) -> dict:
    """The audit trail as a live feed — every event `log_event` has recorded.

    `sinceId` lets a poller ask for what's new since the last call rather
    than re-fetching the whole window; pass back the response's own
    `nextSinceId` on the following call.
    """
    events = ledger.list_events(agent_id=agentId, limit=limit, since_id=sinceId)
    return {"events": events, "nextSinceId": events[0]["id"] if events else sinceId}


@app.get("/health")
def health() -> JSONResponse:
    """Liveness, plus whether the ledger is actually reachable.

    Added after a Supabase project went away underneath a running deployment
    and every ledger-backed endpoint started returning a bare 500. The
    service was up, the database was not, and nothing distinguished those two
    cases from outside — which is the difference between a five-minute fix and
    an afternoon.

    The probe is a `SELECT 1`, so it costs a round trip and no table access.
    A failing ledger makes this endpoint 503 rather than 200-with-a-sad-field:
    a load balancer or uptime check reads the status code, and a facilitator
    that cannot write commitments is not healthy in any useful sense.
    """
    ledger_ok, ledger_detail = ledger.check_connection()

    body = {
        "service": "facilitator",
        "status": "ok" if ledger_ok else "degraded",
        "settlementMode": SETTLEMENT_MODE,
        "razorpayMode": gateway.mode,
        "ledger": {
            "engine": ledger.dialect,
            "reachable": ledger_ok,
            # Scrubbed again at the boundary, even though `check_connection`
            # already does it. This string comes from a database driver and
            # goes out over HTTP; one redaction is a behaviour, two is an
            # invariant that survives someone changing either side.
            "detail": redact_credentials(ledger_detail),
        },
        "webhooksConfigured": bool(RAZORPAY_WEBHOOK_SECRET),
        "proofScheme": ALGORITHM,
        "hmacFallbackAllowed": ALLOW_HMAC_FALLBACK,
    }
    return JSONResponse(status_code=200 if ledger_ok else 503, content=body)


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
        "identityEndpoints": ["/agents/register", "/agents/{agentId}"],
        "webhookEndpoints": ["/webhooks/razorpay"],
        "operational": ["/health", "/ledger/summary", "/economics", "/ledger/events"],
        "demoApi": ["/demo/run"] if ENABLE_DEMO_API else [],
        "proofScheme": ALGORITHM,
    }


# ---------------------------------------------------------------------------
# Vercel path-prefix hedge
# ---------------------------------------------------------------------------
#
# On Vercel this service sits behind a rewrite from /api/facilitator/* on the
# public URL (see vercel.json). Vercel's own documented mechanism for
# stripping that prefix before a service sees it — a `request.path` transform
# in the service's own `routes` — did not do so in practice: every
# /api/facilitator/* path 404s from FastAPI's own not-found handler, meaning
# the request arrives with the prefix still attached. Rather than depend on
# that (still-experimental) platform feature, this mounts the whole app under
# the prefix directly, which is ordinary ASGI and entirely under this
# service's own control.
#
# Unset (the default, everywhere except Vercel) this changes nothing: `app`
# is exactly what it already was, and every test in this project exercises
# that `app` directly.
_BASE_PATH = os.getenv("FACILITATOR_BASE_PATH", "").strip()
if _BASE_PATH:
    _inner_app = app
    app = FastAPI()
    app.mount(_BASE_PATH, _inner_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
