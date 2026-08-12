"""Bharat x402 — server-side agent runner for the console UI.

`demo-agent/crawler_agent.py` narrates the x402 negotiation to a terminal.
This module runs the same negotiation and returns it as structured JSON, so a
browser can render the same five steps without a terminal in the loop.

---------------------------------------------------------------------------
WHY THIS LIVES HERE, AND WHY IT CANNOT LIVE IN THE BROWSER
---------------------------------------------------------------------------
Step 3 of the negotiation — signing acceptance of a quote — needs the shared
HMAC secret. Doing that in page JavaScript would ship the secret to every
visitor's browser, which is a materially worse mistake than any of the other
simplifications this project makes elsewhere. So this endpoint holds the
secret and does the signing; the browser only ever sees the result.

Steps 1 and 4 are genuine cross-service HTTP calls to the resource server —
the same requests `crawler_agent.py` makes — so the 402 and the settlement
receipt shown in the console are real wire traffic, not fabricated. Steps 2
and 3 run in-process against this facilitator's own ledger and signer rather
than looping back over HTTP to itself: `/offer`'s logic is a handful of
already-tested calls (`build_offer`, `sign`, `ledger.insert_offer`), and a
self-HTTP-call would only add a network hop and a cold-start dependency for
no benefit. Verification and settlement (steps inside step 4) still go
through the real `/verify` and `/settle` endpoints — but as calls made *by
the resource server's x402 middleware*, exactly as they are for the CLI agent
and for any other x402 client. Nothing about the protocol path is faked.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from payment_verifier import VerificationError, agent_commitment_body, build_offer, sign
from pydantic import BaseModel, Field

router = APIRouter(prefix="/demo", tags=["demo"])

# Populated by configure(), called once from main.py at startup. A plain dict
# rather than module-level globals so tests can swap it out without patching
# individual names.
_ctx: dict[str, Any] = {}


def configure(
    *,
    ledger: Any,
    hmac_secret: str,
    offer_policy: Any,
    resource_url: str,
) -> None:
    """Wires this router to the facilitator's already-configured objects.

    Called from `main.py` rather than importing `main` here, which would be
    circular — `main.py` is what includes this router in the first place.

    Args:
        ledger: The facilitator's `Ledger` instance.
        hmac_secret: The shared signing secret.
        offer_policy: `OfferPolicy` used for quoting.
        resource_url: Base URL of the resource server, e.g. http://localhost:3402.
    """
    _ctx.update(
        ledger=ledger,
        hmac_secret=hmac_secret,
        offer_policy=offer_policy,
        resource_url=resource_url.rstrip("/"),
    )


# Mirrors the paths registered in resource-server/x402-config.js. The two
# files have to agree on this by hand; there is no shared schema between a
# Node service and a Python one to enforce it automatically.
RESOURCE_PATHS = {
    "market-report": "/premium/market-report",
    "api-call": "/premium/api-call",
}

# Enforced on the agent id the console builds server-side (see run_demo
# below), not on ids arriving from other callers like the CLI agent or the
# test suite — those already produce ids of this shape, but nothing forces
# them to, and nothing should: this validation exists because the console
# writes a session-derived id into the ledger where other visitors see it,
# and that is the one path an arbitrary browser can reach.
AGENT_ID_RE = re.compile(r"^agent-[a-z0-9-]{1,48}$")

# In-memory per-session throttle. A public endpoint that writes a ledger row
# on demand is a way to fill a database for free otherwise. Module-global and
# process-local — fine for a single long-running process, and explicitly not
# fine across multiple serverless instances, which would need a shared store
# (the Postgres ledger itself, most likely) to enforce this for real.
_RATE_LIMIT = 20
_RATE_WINDOW_SECONDS = 60.0
_rate_log: dict[str, list[float]] = {}


def _rate_limited(session_key: str) -> bool:
    """True if `session_key` has exceeded the throttle."""
    now = time.monotonic()
    hits = [t for t in _rate_log.get(session_key, []) if now - t < _RATE_WINDOW_SECONDS]
    hits.append(now)
    _rate_log[session_key] = hits
    return len(hits) > _RATE_LIMIT


class DemoRunRequest(BaseModel):
    """What the console posts to run one negotiation."""

    sessionId: str = Field(..., min_length=4, max_length=32, description="Per-browser identity.")
    agentLabel: str = Field(
        default="perplexity-bot", max_length=32, description="Which sample crawler this looks like."
    )
    resource: str = Field(default="market-report", description="Key into RESOURCE_PATHS.")
    tamper: bool = Field(
        default=False,
        description="Corrupt the signature before presenting it — the 'forge' button.",
    )


_HEADERS_WORTH_SHOWING = {
    "payment-required",
    "payment-response",
    "x-payment-response",
    "content-type",
}


def _visible_headers(headers: httpx.Headers) -> dict[str, str]:
    """The subset of response headers worth putting in a trace.

    Not all of them — `date`, `etag`, `keep-alive` are real headers on a real
    response, but they are noise for someone trying to understand the
    protocol, and a full dump would half-bury the two headers that matter.
    """
    return {k: v for k, v in headers.items() if k.lower() in _HEADERS_WORTH_SHOWING}


def _decode_b64_json(value: str | None) -> Any | None:
    """Decodes a base64+JSON header value, or None if absent/unparseable."""
    if not value:
        return None
    try:
        return json.loads(base64.b64decode(value))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _build_agent_id(session_id: str, agent_label: str) -> str:
    """Turns a session id and a chosen label into a valid, ledger-safe agent id."""
    session = re.sub(r"[^a-z0-9]", "", session_id.lower())[:12] or "anon"
    label = re.sub(r"[^a-z0-9-]", "", agent_label.lower()).strip("-")[:24] or "agent"
    return f"agent-{label}-{session}"


@router.post("/run")
def run_demo(request: DemoRunRequest) -> dict:
    """Runs one full x402 negotiation and returns it as a step-by-step trace.

    Returns:
        `{agentId, ok, amountPaise, elapsedMs, steps[], content, receipt}`.
        `steps[]` carries, per step: a title, a status, the verbatim request/
        response the network actually saw where there was one, and a
        `decoded` block pairing that with what it means. `content`/`receipt`
        are null on failure.
    """
    if not _ctx:
        raise HTTPException(503, "Demo API is not configured.")

    if _rate_limited(request.sessionId):
        raise HTTPException(429, "Too many demo runs from this session. Wait a moment.")

    agent_id = _build_agent_id(request.sessionId, request.agentLabel)
    if not AGENT_ID_RE.match(agent_id):
        raise HTTPException(400, "Could not build a valid agent id from the given session/label.")

    path = RESOURCE_PATHS.get(request.resource, RESOURCE_PATHS["market-report"])
    url = f"{_ctx['resource_url']}{path}"

    started = time.monotonic()
    steps: list[dict] = []

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def failure(reason: str) -> dict:
        return {
            "agentId": agent_id,
            "ok": False,
            "amountPaise": None,
            "elapsedMs": elapsed_ms(),
            "steps": steps,
            "content": None,
            "receipt": None,
            "error": reason,
        }

    with httpx.Client(timeout=20.0) as client:
        # --- Step 1: request unpaid, read the offer -------------------------
        try:
            unpaid = client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            steps.append(
                {
                    "n": 1,
                    "title": "Request the resource with no payment attached",
                    "status": "unreachable",
                    "request": {"method": "GET", "url": url, "headers": {}},
                }
            )
            return failure(f"could not reach the resource server: {exc}")

        required = _decode_b64_json(unpaid.headers.get("payment-required"))
        steps.append(
            {
                "n": 1,
                "title": "Request the resource with no payment attached",
                "status": "ok" if unpaid.status_code == 402 and required else "unexpected",
                "request": {"method": "GET", "url": url, "headers": {}},
                "response": {
                    "status": unpaid.status_code,
                    "headers": _visible_headers(unpaid.headers),
                },
                "decoded": {"field": "PAYMENT-REQUIRED header", "value": required},
                "note": "scheme is razorpay-inr, not 'exact' — this is not an EVM transfer",
            }
        )
        if unpaid.status_code != 402 or not required:
            return failure("resource did not return a 402 offer")

        accepted = required["accepts"][0]
        amount_paise = int(accepted["amount"])
        pay_to = accepted["payTo"]
        # Same fallback crawler_agent.py uses: the 402 does not carry a
        # resourceId today, only the URL does.
        resource_id = path.rstrip("/").rsplit("/", 1)[-1]

        # --- Step 2: quote, in-process ---------------------------------------
        try:
            offer_body = build_offer(
                agent_id=agent_id,
                resource_id=resource_id,
                amount_paise=amount_paise,
                pay_to=pay_to,
                policy=_ctx["offer_policy"],
                resource_url=url,
            )
        except VerificationError as exc:
            steps.append(
                {
                    "n": 2,
                    "title": "Ask the facilitator to quote this fetch",
                    "status": "failed",
                    "decoded": {"reason": exc.reason, "message": exc.message},
                }
            )
            return failure(exc.message)

        signature = sign(offer_body, _ctx["hmac_secret"])
        _ctx["ledger"].insert_offer(offer_body, signature)
        commitment_template = agent_commitment_body(offer_body, "<acceptedAt>")

        steps.append(
            {
                "n": 2,
                "title": "Ask the facilitator to quote this fetch",
                "status": "ok",
                "decoded": {
                    "offer": offer_body,
                    "signature": signature,
                    "commitmentTemplate": commitment_template,
                },
                "note": (
                    "An agent paying in USDC would skip this — it holds a wallet and "
                    "signs a transfer itself. Paying in rupees, it has to be quoted."
                ),
            }
        )

        # --- Step 3: sign acceptance, in-process -----------------------------
        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        commitment = dict(commitment_template)
        commitment["acceptedAt"] = accepted_at
        agent_signature = sign(commitment, _ctx["hmac_secret"])

        if request.tamper:
            # Flip one hex character. Still a well-formed 64-char signature —
            # the point is to fail verification, not to fail parsing.
            flipped = "1" if agent_signature[0] == "0" else "0"
            agent_signature = flipped + agent_signature[1:]

        canonical = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
        steps.append(
            {
                "n": 3,
                "title": "Sign acceptance of the quote",
                "status": "ok",
                "decoded": {
                    "canonicalJson": canonical,
                    "signature": agent_signature,
                    "algorithm": "HMAC-SHA256",
                    "tampered": request.tamper,
                },
                "note": (
                    "Signed server-side — the shared secret never reaches your browser. "
                    "Demo simplification: HMAC with a secret shared with the facilitator. "
                    "Production would use a per-agent keypair."
                ),
            }
        )

        payment_payload = {
            "offerId": offer_body["offerId"],
            "agentId": agent_id,
            "acceptedAt": accepted_at,
            "agentSignature": agent_signature,
        }
        envelope = {
            "x402Version": required["x402Version"],
            "accepted": accepted,
            "payload": payment_payload,
        }
        x_payment = base64.b64encode(json.dumps(envelope).encode()).decode()

        # --- Step 4: retry with payment ---------------------------------------
        try:
            paid = client.get(
                url, headers={"X-PAYMENT": x_payment, "Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            steps.append(
                {
                    "n": 4,
                    "title": "Retry the request with the payment attached",
                    "status": "unreachable",
                }
            )
            return failure(f"could not reach the resource server: {exc}")

        steps.append(
            {
                "n": 4,
                "title": "Retry the request with the payment attached",
                "status": "ok" if paid.status_code == 200 else "failed",
                "request": {
                    "method": "GET",
                    "url": url,
                    "headers": {"X-PAYMENT": x_payment},
                },
                "response": {"status": paid.status_code, "headers": _visible_headers(paid.headers)},
            }
        )

        receipt = _decode_b64_json(
            paid.headers.get("payment-response") or paid.headers.get("x-payment-response")
        )

        if paid.status_code != 200:
            retry_required = _decode_b64_json(paid.headers.get("payment-required"))
            reason = (retry_required or {}).get("error", "payment rejected")
            steps.append(
                {
                    "n": 5,
                    "title": "Read the settlement receipt",
                    "status": "failed",
                    "decoded": {"reason": reason},
                }
            )
            return failure(reason)

        steps.append(
            {
                "n": 5,
                "title": "Read the settlement receipt",
                "status": "ok",
                "decoded": {"field": "PAYMENT-RESPONSE header", "value": receipt},
            }
        )

        content = paid.json()

    return {
        "agentId": agent_id,
        "ok": True,
        "amountPaise": amount_paise,
        "elapsedMs": elapsed_ms(),
        "steps": steps,
        "content": content,
        "receipt": receipt,
    }
