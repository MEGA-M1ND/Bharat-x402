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

import consent
from consent import ConsentDenied
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from identity import (
    SCOPE_ECONOMICS_READ,
    SCOPE_EVENTS_READ,
    SCOPE_LEDGER_READ,
    SCOPE_SETTLE_WRITE,
)
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

# Identifier for this facilitator's own x402 extension, advertised on
# /supported and echoed in the /settle response.
#
# The x402 spec (v2 §7.3.1) treats `extensions` as a list of identifiers a
# facilitator implements. Ours declares one thing: settlement is deferred, so
# a successful /settle records a receivable and moves no money. Namespaced by
# reverse-domain and versioned, so it cannot collide with a future standard
# extension and a client can pin the shape it parses.
#
# See docs/protocol-extension.md.
DEFERRED_EXTENSION_ID = "in.bharatx402.deferred-settlement/v1"

# Whether a request must hold reserved authority before content is released.
# Phase 3 makes this meaningful; declared here so /supported can advertise it
# from the start rather than changing shape later.
AUTHORITY_REQUIRED = os.getenv("AUTHORITY_REQUIRED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


# Whether the *public dashboard reads* — /ledger/summary, /economics,
# /ledger/events — may be called without an API key.
#
# DEFAULT IS CLOSED. This is the single most consequential default in the
# service: before Phase 2 these endpoints were unconditionally public and
# returned any named agent's spend, any publisher's revenue, and the entire
# audit trail to anyone who asked. An `agentId` query parameter is a filter,
# and a filter is not authorization.
#
# The hosted demo turns this on deliberately, because a browser cannot hold a
# secret and the whole point of that deployment is that a stranger can click
# it. That is a real trade, made once, in one place, with a startup warning —
# not a permissive default that nobody notices. Writes are never opened by it:
# /settle-batch and the entire /control plane always require a key.
DEMO_OPEN_DASHBOARD = _flag("DEMO_OPEN_DASHBOARD", "false")

# Whether an unauthenticated caller may still bind a public key to an agent id
# by being first (`POST /agents/register`).
#
# DEFAULT IS CLOSED. Trust-on-first-use proves key continuity, not identity,
# and "first caller wins" is an identity-claiming vulnerability the moment an
# agent id is worth anything. Enrollment now runs through an authenticated
# operator and a challenge-response proof of possession — see
# control_plane.enroll_agent.
DEMO_UNSAFE_TOFU = _flag("DEMO_UNSAFE_TOFU", "false")

# Whether an agent must hold an active operator consent to spend at all.
#
# DEFAULT IS ON. This is the Phase 2 thesis in one flag: x402 establishes that
# an agent agreed to a price, and that is not the same as being allowed to
# spend somebody's money. With this on, an agent with no consent is refused at
# quote time — before it signs anything and before a ledger row exists.
#
# Off, the facilitator books commitments against pseudonymous keys with nobody
# on the hook for them, which is what the original design did.
REQUIRE_CONSENT = _flag("REQUIRE_CONSENT", "true")

# The publisher these quotes are for, when the deployment serves one. Consents
# can be scoped to named merchants; with no merchant identified, a consent
# that names any merchant will refuse rather than assume this is one of them.
FACILITATOR_MERCHANT_ID = (os.getenv("FACILITATOR_MERCHANT_ID") or "").strip() or None

HMAC_SECRET = os.getenv("FACILITATOR_HMAC_SECRET", "dev-only-shared-secret-change-me")

# Whether an agent with no registered Ed25519 key may still pay with a
# shared-secret HMAC proof.
#
# DEFAULT IS NOW FALSE. This was `true` while Ed25519 keys were rolling out —
# a migration ramp so existing clients kept working. A ramp that never ends is
# just a permanently weaker default, and a shared secret cannot provide
# non-repudiation no matter how strong the MAC is: the facilitator holds the
# same key the agent does, so it could mint any agent's acceptance itself. See
# payment_verifier.py.
#
# The demo profile may still set it true; the production-like default requires
# a registered key.
ALLOW_HMAC_FALLBACK = os.getenv("ALLOW_HMAC_FALLBACK", "false").strip().lower() in (
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

import control_plane  # noqa: E402 - same reason: needs `ledger` on app.state first

# The control plane reaches the ledger through app state rather than importing
# it from here, which keeps it importable on its own and lets the tests mount
# it against a throwaway database.
app.state.ledger = ledger
app.include_router(control_plane.router)

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

        # Every relaxed security default, named, at startup.
        #
        # A permissive setting that nobody notices is the whole failure mode
        # these flags exist to avoid, and the one that had actually happened
        # here: the HMAC fallback shipped as a migration ramp and was still on
        # long after the migration finished, because nothing ever said so. A
        # deployment that is running open now has to say it out loud, once per
        # boot, in the same structured log everything else goes to.
        relaxed = {
            "DEMO_OPEN_DASHBOARD": DEMO_OPEN_DASHBOARD,
            "DEMO_UNSAFE_TOFU": DEMO_UNSAFE_TOFU,
            "ALLOW_HMAC_FALLBACK": ALLOW_HMAC_FALLBACK,
            "REQUIRE_CONSENT_DISABLED": not REQUIRE_CONSENT,
        }
        enabled = sorted(name for name, on in relaxed.items() if on)
        if enabled:
            ledger.log_event(
                "insecure_demo_mode_enabled",
                status="warning",
                flags=" ".join(enabled),
                message=(
                    "Running with relaxed defaults for the public demo. Each of these is "
                    "closed in the production-like profile; see docs/threat-model.md."
                ),
            )
        else:
            ledger.log_event(
                "security_profile",
                status="ok",
                profile="production-like",
                message=(
                    "Dashboard reads require an API key, key enrollment requires an "
                    "authenticated operator, HMAC fallback is off, and an operator "
                    "consent is required to spend."
                ),
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
                    # The single most important field here, and the reason
                    # this scheme needs an extension at all.
                    #
                    # In the reference `exact` scheme, a successful /settle
                    # has broadcast a transfer — funds moved. Here it has
                    # booked a receivable and moved nothing. A client that
                    # cannot tell those apart will report a debt to its user
                    # as a completed payment.
                    #
                    # Expressed as a boolean rather than left to prose in a
                    # README, so a client can branch on it. See
                    # docs/protocol-extension.md.
                    "fundsMoveAtSettle": False,
                    "settlementTiming": "batched",
                    "authorityRequired": AUTHORITY_REQUIRED,
                    # payment_link | reserve_pay. A client that cares whether
                    # settlement involves a page a human opens can see it here.
                    "settlementInstrument": gateway.instrument,
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
        # Spec v2 §7.3.1: "Array of extension identifiers the facilitator has
        # implemented." None of the upstream extensions (bazaar,
        # sign-in-with-x, ...) are implemented — this is our own, declaring
        # that settlement here is deferred rather than immediate.
        #
        # Namespaced and versioned so it cannot collide with a future
        # standard extension, and so a client can pin the shape it understands.
        "extensions": [DEFERRED_EXTENSION_ID],
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

    Reads `agent_credentials`, not `agents`. The credential table is where
    status and validity live, and `get_active_agent_credential` filters on
    both in SQL — so a revoked or expired key is simply not returned, and a
    payment presented under it fails as `agent_not_registered` rather than
    being cheerfully verified against a key an operator has withdrawn.

    Returning None here is what makes revocation *bite*: it drops the caller
    onto the "no key on file" branch, which under the production default
    (`ALLOW_HMAC_FALLBACK=false`) is a refusal.
    """
    if offer_row is None:
        return None
    return ledger.get_active_agent_credential(offer_row["agent_id"])


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

    # Trust-on-first-use, gated.
    #
    # This endpoint binds a public key to an agent id on the strength of
    # nothing but arriving first. That was defensible when an agent id was
    # worth nothing; it stops being defensible the moment an id carries a
    # spending consent, because claiming the id is then claiming the money.
    #
    # The replacement is POST /control/agents/enroll: an authenticated
    # operator, a server-issued nonce, and a signature proving possession of
    # the private half. This path survives only for the public demo, where
    # there is no operator to authenticate as.
    if not DEMO_UNSAFE_TOFU:
        return JSONResponse(
            status_code=403,
            content={
                "error": "tofu_disabled",
                "message": (
                    "Unauthenticated key registration is disabled. Enroll through "
                    "POST /control/agents/challenge then POST /control/agents/enroll "
                    "with an operator API key, which proves possession of the private "
                    "key instead of trusting whoever asks first."
                ),
                "enrollmentEndpoint": "/control/agents/enroll",
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


def _authorize_spend(*, agent_id: str, amount_paise: int) -> tuple[str | None, str | None]:
    """Decides whether this agent may incur this expense at all.

    The question x402 does not ask. A signed acceptance proves a key agreed to
    a price; it says nothing about whether that key was ever authorised to
    spend anyone's money. This is where an operator's consent — with its
    per-request, daily, and lifetime limits, its publisher scope, and its
    revocability — is actually enforced.

    Called at BOTH `/offer` and `/settle`, deliberately. Quote time is where a
    refusal is most useful (the agent finds out before it signs, and no row is
    written); settle time is where it is *binding*, because the two are
    separate HTTP requests and a consent can be revoked between them.

    Args:
        agent_id: The agent proposing to spend.
        amount_paise: The quoted amount, integer paise.

    Returns:
        `(operator_id, consent_id)` to stamp on the commitment, or
        `(None, None)` when consent is not required and none exists — the
        legacy path, where nobody is on the hook but a pseudonymous key.

    Raises:
        ConsentDenied: On any refusal. Never falls through to allowed.
    """
    consent_row = ledger.active_consent_for_agent(agent_id)

    if consent_row is None:
        if not REQUIRE_CONSENT:
            return None, None
        raise ConsentDenied(
            "no_consent",
            f"agent {agent_id} has no active spending consent. An operator must authorise "
            "it via POST /control/consents before it can incur an expense.",
        )

    decision = consent.evaluate(
        consent=consent_row,
        operator=ledger.get_operator(consent_row["operator_id"]),
        agent=ledger.get_agent(agent_id),
        merchant_id=FACILITATOR_MERCHANT_ID,
        scoped_merchant_ids=ledger.consent_merchant_scope(consent_row["consent_id"]),
        amount_paise=amount_paise,
        committed_today_paise=ledger.committed_against_consent(
            consent_id=consent_row["consent_id"], settle_date=today_utc()
        ),
    )
    return decision.operator_id, decision.consent_id


def _consent_refusal(exc: ConsentDenied, *, agent_id: str, amount_paise: int) -> JSONResponse:
    """Records and renders a consent refusal in the x402 failure shape."""
    ledger.log_event(
        "spend_refused_by_consent",
        agent_id=agent_id,
        amount_paise=amount_paise,
        status="rejected",
        reason=exc.reason,
        message=exc.message,
        **exc.detail,
    )
    return JSONResponse(
        status_code=200,
        content={
            "success": False,
            "errorReason": exc.reason,
            "errorMessage": exc.message,
            "transaction": "",
            "network": X402_NETWORK,
        },
    )


def _deferred_extension(
    *,
    commitment_id: str,
    settle_date: str,
    collected: bool = False,
    replayed: bool = False,
) -> dict:
    """The lifecycle block echoed under `extensions` in a settle response.

    Five booleans instead of one overloaded `success`, because "the request was
    authorized", "the content was delivered", "a debt exists", and "money
    arrived" are four different questions and a client that cannot tell them
    apart will show a user a receivable labelled as a completed payment.

    A conforming upstream client that ignores `extensions` still works — it
    reads `success` and `transaction` as usual. That is the compatibility
    guarantee. This block is for clients that want the truth.

    Args:
        commitment_id: The receivable this settlement recorded.
        settle_date: The batching key the commitment was booked against.
        collected: Whether a gateway confirmation has already landed. Almost
            always False here: collection happens in a later batch run.
        replayed: Whether this response returned a pre-existing commitment
            rather than creating one.

    Returns:
        A dict suitable for `extensions[DEFERRED_EXTENSION_ID]`.
    """
    return {
        DEFERRED_EXTENSION_ID: {
            "authorized": True,
            "fulfilled": True,
            "committed": True,
            # The publisher is owed money and has not received it. This is the
            # field that distinguishes this scheme from `exact`.
            "collectionPending": not collected,
            "collected": collected,
            "fundsMoved": collected,
            "commitmentId": commitment_id,
            "settleDate": settle_date,
            "replayed": replayed,
        }
    }


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
                "extensions": _deferred_extension(
                    commitment_id=existing["commitment_id"],
                    settle_date=existing["settle_date"],
                    replayed=True,
                ),
            },
        )

    # The binding consent check. /offer already refused the obvious cases, but
    # that was a different HTTP request: a consent can be suspended, revoked,
    # or exhausted between the quote and its use, and the answer that counts
    # is the one at the moment the receivable is booked.
    try:
        operator_id, consent_id = _authorize_spend(
            agent_id=result["agentId"], amount_paise=result["amountPaise"]
        )
    except ConsentDenied as exc:
        return _consent_refusal(
            exc, agent_id=result["agentId"], amount_paise=result["amountPaise"]
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
            operator_id=operator_id,
            consent_id=consent_id,
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
                    "amount": str(existing["amount_paise"]),
                    "extra": {"replayed": True},
                    "extensions": _deferred_extension(
                        commitment_id=existing["commitment_id"],
                        settle_date=existing["settle_date"],
                        replayed=True,
                    ),
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
            "extensions": _deferred_extension(
                commitment_id=commitment["commitmentId"],
                settle_date=commitment["settleDate"],
            ),
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
        link = gateway.create_charge(
            amount_paise=commitment["amountPaise"],
            description=f"x402 request: {commitment['resourceId']}",
            reference_id=batch_id,
            agent_id=commitment["agentId"],
            notes={"commitment_id": commitment["commitmentId"], "mode": "per_request"},
            already_debited_paise=ledger.debited_today(
                agent_id=commitment["agentId"], settle_date=commitment["settleDate"]
            ),
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
        instrument=link.get("instrument", "payment_link"),
        # A mandate debit has already taken the money; a Payment Link has not,
        # and waits for its webhook. See Ledger.record_batch.
        amount_paid_paise=(
            commitment["amountPaise"] if link.get("status") == "captured" else None
        ),
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

    # Operator consent, before anything is written.
    #
    # Quote time is where a refusal is most useful — the agent learns its
    # authority is missing or exhausted before it signs, and no ledger row is
    # created. It is re-checked at /settle, which is where the decision
    # actually binds: the two are separate HTTP requests and consent can be
    # revoked in between.
    try:
        _authorize_spend(agent_id=request.agentId, amount_paise=request.amountPaise)
    except ConsentDenied as exc:
        ledger.log_event(
            "offer_refused_by_consent",
            agent_id=request.agentId,
            resource_id=request.resourceId,
            amount_paise=request.amountPaise,
            status="rejected",
            reason=exc.reason,
            message=exc.message,
            **exc.detail,
        )
        return JSONResponse(
            status_code=403,
            content={"error": exc.reason, "message": exc.message, **exc.detail},
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
def settle_batch(request: BatchRequest, http_request: Request) -> JSONResponse:
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
    # Settlement is a WRITE that creates real gateway charges against real
    # commitments, so unlike the dashboard reads it is never opened by the demo
    # profile. `settle:write` is required whenever a control plane is
    # reachable; the demo deployment reaches it through the session-scoped
    # agentId path below, which cannot sweep anyone else's commitments.
    authorization = http_request.headers.get("authorization")
    if authorization or not DEMO_OPEN_DASHBOARD:
        try:
            principal = control_plane.authenticate(ledger, authorization)
            principal.require(SCOPE_SETTLE_WRITE)
            if principal.operator_id:
                request.agentId = _tenant_agent_filter(principal, request.agentId)
        except control_plane.AuthError as exc:
            return control_plane.auth_error_response(exc)

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
            # Instrument-agnostic: this becomes a hosted Payment Link or a UPI
            # Reserve Pay mandate debit depending on SETTLEMENT_INSTRUMENT, and
            # nothing here needs to know which. See razorpay_client.create_charge.
            link = gateway.create_charge(
                amount_paise=total,
                description=description,
                reference_id=batch_id,
                agent_id=agent_id,
                notes={
                    "settle_date": settle_date,
                    "commitments": str(len(commitment_ids)),
                },
                # A mandate is drawn down across the day, so a debit needs to
                # know what has already been taken. Reads the ledger rather
                # than a tally kept in the gateway, which could disagree with it.
                already_debited_paise=ledger.debited_today(
                    agent_id=agent_id, settle_date=settle_date
                ),
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
            instrument=link.get("instrument", "payment_link"),
            amount_paid_paise=(total if link.get("status") == "captured" else None),
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


def _authorize_read(authorization: str | None, scope: str) -> control_plane.Principal | None:
    """Gates a dashboard read.

    Returns the authenticated principal, or None when the demo profile has
    opened these reads and no credential was presented.

    The shape matters: `None` means "unauthenticated, and that is permitted
    here", which the caller must handle by scoping the read some other way. It
    never means "treat as authorized". A caller that ignores the return value
    still gets the exception on the closed path.

    Raises:
        AuthError: When authentication is required and absent or invalid, or
            when the presented key lacks `scope`.
    """
    if DEMO_OPEN_DASHBOARD and not authorization:
        return None
    principal = control_plane.authenticate(ledger, authorization)
    principal.require(scope)
    return principal


def _tenant_agent_filter(
    principal: control_plane.Principal | None, requested_agent_id: str | None
) -> str | None:
    """Resolves which agent's data a caller may actually see.

    The rule that closes the cross-tenant hole: **an operator key can only
    ever read its own agents.** A requested `agentId` may narrow that, never
    widen it — so `?agentId=someone-elses-agent` on an operator key resolves
    to that operator's own scope and returns nothing belonging to the other
    party, rather than being honoured.

    Merchant keys read publisher-side aggregates and are not agent-scoped, so
    for them the requested filter is passed through unchanged.

    Args:
        principal: The authenticated caller, or None under the open demo
            profile.
        requested_agent_id: The `agentId` query parameter, if any.

    Returns:
        The agent id to filter on, or None for an unfiltered read.

    Raises:
        AuthError: If an operator key asks for an agent it does not own.
    """
    if principal is None:
        # Demo profile: the console partitions by session-derived agent id.
        # This is not authorization and is not presented as any.
        return requested_agent_id

    if principal.merchant_id:
        return requested_agent_id

    operator_id = principal.operator_id
    owned = {a["agentId"] for a in ledger.list_agents_for_operator(operator_id)}

    if requested_agent_id is None:
        # No filter asked for. An operator with exactly one agent gets it;
        # with several, `None` would read *everyone's* data, so refuse rather
        # than guess.
        if len(owned) == 1:
            return next(iter(owned))
        raise control_plane.AuthError(
            "agent_id_required",
            "This operator has "
            f"{len(owned)} agents. Name one with ?agentId= — an unfiltered read would "
            "cross tenants.",
            400,
        )

    if requested_agent_id not in owned:
        raise control_plane.AuthError(
            "not_your_agent",
            f"agent {requested_agent_id} does not belong to this operator",
            403,
        )
    return requested_agent_id


@app.get("/ledger/summary")
def ledger_summary(
    request: Request,
    settleDate: str | None = None,
    agentId: str | None = None,
) -> JSONResponse:
    """One day's activity, for the reporting script and the dashboard.

    Requires `ledger:read` unless the demo profile has opened dashboard reads.
    The agent filter is resolved from the *key*, not from the query string —
    see `_tenant_agent_filter` for why that distinction is the whole fix.
    """
    try:
        principal = _authorize_read(request.headers.get("authorization"), SCOPE_LEDGER_READ)
        agent_filter = _tenant_agent_filter(principal, agentId)
    except control_plane.AuthError as exc:
        return control_plane.auth_error_response(exc)

    return JSONResponse(
        status_code=200, content=ledger.daily_summary(settleDate, agent_id=agent_filter)
    )


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
def economics(
    request: Request, settleDate: str | None = None, agentId: str | None = None
) -> JSONResponse:
    """The batching-versus-per-request comparison, as a read.

    `/settle-batch` computes the same figures but only ever sees commitments
    still `pending` — so a card built the same way would go blank the instant
    a batch clears. This looks at every commitment for the day regardless of
    status, so the number stays put after settling.

    Requires `economics:read` unless the demo profile has opened dashboard
    reads.
    """
    try:
        principal = _authorize_read(request.headers.get("authorization"), SCOPE_ECONOMICS_READ)
        agentId = _tenant_agent_filter(principal, agentId)
    except control_plane.AuthError as exc:
        return control_plane.auth_error_response(exc)

    settle_date = settleDate or today_utc()
    amounts = ledger.commitment_amounts(agent_id=agentId, settle_date=settle_date)
    return JSONResponse(status_code=200, content={
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
    })


@app.get("/ledger/events")
def ledger_events(
    request: Request,
    agentId: str | None = None,
    limit: int = 50,
    sinceId: int | None = None,
) -> JSONResponse:
    """The audit trail as a live feed — every event `log_event` has recorded.

    `sinceId` lets a poller ask for what's new since the last call rather
    than re-fetching the whole window; pass back the response's own
    `nextSinceId` on the following call.

    Requires `events:read` unless the demo profile has opened dashboard reads.
    This is the most sensitive of the three: the audit trail carries every
    agent's activity, so an unscoped read of it is a full disclosure of who is
    buying what from whom.
    """
    try:
        principal = _authorize_read(request.headers.get("authorization"), SCOPE_EVENTS_READ)
        agentId = _tenant_agent_filter(principal, agentId)
    except control_plane.AuthError as exc:
        return control_plane.auth_error_response(exc)

    events = ledger.list_events(agent_id=agentId, limit=limit, since_id=sinceId)
    return JSONResponse(
        status_code=200,
        content={"events": events, "nextSinceId": events[0]["id"] if events else sinceId},
    )


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
        "settlementInstrument": gateway.instrument,
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
