"""Razorpay webhook intake — the half of settlement that closes the loop.

`/settle-batch` creates a Payment Link and stops. That is an invoice, not a
receipt, and until this module existed the ledger had no way to learn that
money actually arrived: a batch sat at `created` forever and the publisher's
"revenue" figure was really a "billed" figure wearing revenue's clothes.

This is the callback Razorpay makes when a link is paid, expires, or is
cancelled, and it is the only code path that moves a batch to `paid`.

---------------------------------------------------------------------------
WHY THIS ENDPOINT IS THE MOST SECURITY-SENSITIVE ONE IN THE SERVICE
---------------------------------------------------------------------------
Every other write here is reached through a payment proof the facilitator
itself issued and can re-derive. This one is an unauthenticated POST from the
public internet that marks money as received. Get it wrong and anyone who can
guess a `plink_` id can mark their own debts paid.

Three things carry the weight, in order:

  1. **Signature over the raw body.** HMAC-SHA256 with the webhook secret,
     compared in constant time. The bytes signed must be exactly the bytes
     received — parsing to JSON and re-serialising changes whitespace and key
     order and breaks verification, so the handler takes `Request` and reads
     `await request.body()` rather than declaring a Pydantic model. This is
     the single most common way webhook verification is implemented wrong.

  2. **Fail closed with no secret.** If `RAZORPAY_WEBHOOK_SECRET` is unset the
     endpoint refuses every delivery. The tempting alternative — skip
     verification when unconfigured, "just for local dev" — ships an
     unauthenticated ledger-write endpoint to production the first time
     someone forgets an environment variable.

  3. **Exactly-once via a database constraint.** Razorpay retries until it
     gets a 2xx, so duplicates are the normal case. Deliveries are claimed
     with a primary-key INSERT (`ledger.claim_webhook`); a redelivery loses
     that INSERT and returns 200 without touching a batch. Checking "have we
     seen this?" with a SELECT first would let two concurrent retries both
     pass the check.

And one thing that deliberately does *not* carry weight: the amount in the
payload is recorded but never trusted to decide whether the batch is paid.
The link id is what identifies the batch, and Razorpay's own record is
authoritative for the amount.

---------------------------------------------------------------------------
STATUS CODES ARE PART OF THE PROTOCOL HERE
---------------------------------------------------------------------------
Razorpay retries on non-2xx. That makes the status code a control signal, not
decoration:

  * 200 — processed, or safely ignorable. Stop retrying.
  * 400 — bad signature or unparseable body. Retrying will not help, but this
    is also exactly what an attacker probing the endpoint sees, so it says
    nothing beyond "rejected".
  * 503 — the endpoint is not configured. Retrying *might* help, once someone
    sets the secret, so this is the one failure worth retrying.

A webhook for a link this facilitator has never heard of gets a 200, not a
404: it is a real delivery that we have correctly decided to ignore (another
service sharing the Razorpay account, most likely), and answering 404 would
make Razorpay retry it for hours. It is still written to the audit trail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import journal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Set by configure(); this module is only wired up when main.py mounts it.
_ctx: dict[str, Any] = {}

# Events this facilitator acts on. Anything else Razorpay sends is
# acknowledged and logged without a ledger write — an allow-list rather than
# a match/else, so a new Razorpay event type can never fall through into
# unintended handling.
PAID_EVENTS = frozenset({"payment_link.paid"})
VOID_EVENTS = {"payment_link.expired": "expired", "payment_link.cancelled": "cancelled"}


def configure(*, ledger: Any, webhook_secret: str | None) -> None:
    """Wires the router to the facilitator's ledger and webhook secret.

    Args:
        ledger: The `Ledger` instance to write through.
        webhook_secret: Razorpay's webhook signing secret. None or empty
            disables the endpoint entirely — see the module docstring.
    """
    _ctx["ledger"] = ledger
    _ctx["webhook_secret"] = (webhook_secret or "").strip()


def verify_webhook_signature(*, raw_body: bytes, signature: str, secret: str) -> bool:
    """Checks Razorpay's `X-Razorpay-Signature` against the raw request body.

    Args:
        raw_body: The exact bytes received, unparsed.
        signature: Value of the `X-Razorpay-Signature` header.
        secret: The webhook signing secret from the Razorpay dashboard.

    Returns:
        Whether the signature is valid.
    """
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _dedupe_key(*, request: Request, raw_body: bytes) -> str:
    """A stable identifier for one webhook delivery.

    Razorpay sends `X-Razorpay-Event-Id`, which is the right key: the same
    logical event redelivered carries the same id. Falling back to a hash of
    the body covers the case where the header is absent — slightly weaker,
    because two genuinely distinct events with byte-identical payloads would
    collide, but for the events handled here the payload always carries a
    unique link id and timestamp, so identical bytes really do mean a
    redelivery.
    """
    event_id = request.headers.get("x-razorpay-event-id")
    if event_id:
        return f"evt:{event_id}"
    return f"sha256:{hashlib.sha256(raw_body).hexdigest()}"


def _link_id(payload: dict) -> str | None:
    """Digs the Payment Link id out of Razorpay's nested payload shape."""
    return (
        payload.get("payload", {})
        .get("payment_link", {})
        .get("entity", {})
        .get("id")
    )


def _payment_id(payload: dict) -> str | None:
    """The underlying `pay_...` id, when the event carries one."""
    return payload.get("payload", {}).get("payment", {}).get("entity", {}).get("id")


def _amount_paid_paise(payload: dict) -> int:
    """What Razorpay says arrived, in paise."""
    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    return int(entity.get("amount_paid") or 0)


@router.post("/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    """Receives one Razorpay webhook delivery.

    Async purely so the raw body can be awaited before anything parses it —
    the ledger calls inside are the same synchronous ones every other endpoint
    uses.
    """
    ledger = _ctx.get("ledger")
    secret = _ctx.get("webhook_secret") or ""

    if ledger is None or not secret:
        # No secret means no way to tell Razorpay apart from anyone else.
        return JSONResponse(
            status_code=503,
            content={
                "error": "webhooks_not_configured",
                "message": (
                    "This facilitator has no RAZORPAY_WEBHOOK_SECRET set, so webhook "
                    "signatures cannot be verified. The endpoint refuses deliveries "
                    "rather than accepting unauthenticated ledger writes."
                ),
            },
        )

    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not verify_webhook_signature(raw_body=raw_body, signature=signature, secret=secret):
        # Logged without the body: an unverified payload is attacker-controlled
        # and has no business being echoed into the audit trail at full length.
        ledger.log_event(
            "webhook_signature_invalid",
            status="rejected",
            reason="bad_signature",
            bodyBytes=len(raw_body),
            hasSignatureHeader=bool(signature),
        )
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_signature", "message": "Webhook signature check failed."},
        )

    # Only now, after the bytes are proven to be Razorpay's, is it safe to parse.
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        ledger.log_event(
            "webhook_malformed", status="rejected", reason="not_json", bodyBytes=len(raw_body)
        )
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_body", "message": "Signed body was not valid JSON."},
        )

    event = body.get("event", "unknown")
    link_id = _link_id(body)
    dedupe_key = _dedupe_key(request=request, raw_body=raw_body)

    if not ledger.claim_webhook(dedupe_key=dedupe_key, event=event, payment_link_id=link_id):
        # A retry of something already handled. Answering 200 is what stops
        # Razorpay retrying; doing nothing else is what stops it double-counting.
        ledger.log_event(
            "webhook_duplicate_ignored",
            status="ok",
            razorpayEvent=event,
            paymentLinkId=link_id,
            note="Already processed. Acknowledged without re-applying.",
        )
        return JSONResponse(status_code=200, content={"status": "duplicate", "event": event})

    if event in PAID_EVENTS:
        return _apply_paid(ledger, body, event, link_id, dedupe_key)

    if event in VOID_EVENTS:
        return _apply_void(ledger, event, link_id, dedupe_key, VOID_EVENTS[event])

    ledger.finish_webhook(dedupe_key=dedupe_key, outcome="ignored", razorpayEvent=event)
    ledger.log_event(
        "webhook_ignored",
        status="ok",
        # Named `razorpayEvent`, not `event`: `log_event`'s own first
        # parameter is `event`, and passing both collides at the call.
        razorpayEvent=event,
        paymentLinkId=link_id,
        note="Verified and recorded, but this facilitator does not act on this event.",
    )
    return JSONResponse(status_code=200, content={"status": "ignored", "event": event})


def _apply_paid(
    ledger: Any, body: dict, event: str, link_id: str | None, dedupe_key: str
) -> JSONResponse:
    """Marks the batch behind a paid Payment Link as collected."""
    amount_paid = _amount_paid_paise(body)
    payment_id = _payment_id(body)

    batch = (
        ledger.mark_batch_paid(
            payment_link_id=link_id,
            amount_paid_paise=amount_paid,
            razorpay_payment_id=payment_id,
        )
        if link_id
        else None
    )

    if batch is None:
        # Either a link this facilitator never created, or one already marked
        # paid by an earlier delivery with a different event id. Both are
        # acknowledged: see the module docstring on why this is not a 404.
        ledger.finish_webhook(
            dedupe_key=dedupe_key, outcome="unknown_link", paymentLinkId=link_id
        )
        ledger.log_event(
            "webhook_link_not_matched",
            status="ok",
            razorpayEvent=event,
            paymentLinkId=link_id,
            amount_paise=amount_paid,
            note="No unpaid batch matches this link. Acknowledged, nothing changed.",
        )
        return JSONResponse(status_code=200, content={"status": "no_matching_batch"})

    # The journal posting for the only event in this system that turns a
    # receivable into value we hold.
    #
    # `command_ref` keys off the BATCH, not the webhook delivery: Razorpay
    # retries with backoff for 24 hours and duplicates are routine, so a
    # per-delivery key would post a second set of entries for the same money.
    # The webhook_events primary key already makes processing exactly-once;
    # this is the same guarantee at the accounting layer, so the journal is
    # correct even if the two ever disagree.
    #
    # Posted with the amount Razorpay reported, not the amount we billed. If
    # they differ that is a discrepancy for reconciliation to classify, and
    # writing down what we expected instead of what arrived would hide it.
    ledger.post_journal(
        journal.confirm_collection(
            command_ref=f"collect:{batch['batch_id']}",
            amount_paise=amount_paid,
            agent_id=batch["agent_id"],
            batch_id=batch["batch_id"],
        )
    )

    ledger.finish_webhook(
        dedupe_key=dedupe_key,
        outcome="applied",
        batchId=batch["batch_id"],
        amountPaidPaise=amount_paid,
    )
    ledger.log_event(
        "batch_paid",
        agent_id=batch["agent_id"],
        amount_paise=amount_paid,
        status="ok",
        batchId=batch["batch_id"],
        paymentLinkId=link_id,
        razorpayPaymentId=payment_id,
        billedPaise=batch["total_paise"],
        # Worth surfacing rather than silently accepting: a short payment
        # means the batch total and what arrived disagree, which is a
        # reconciliation problem a publisher needs to see.
        shortfallPaise=max(int(batch["total_paise"]) - amount_paid, 0),
        note="Razorpay confirmed payment. This is the only event that means money moved.",
    )
    return JSONResponse(
        status_code=200,
        content={"status": "applied", "batchId": batch["batch_id"], "event": event},
    )


def _apply_void(
    ledger: Any, event: str, link_id: str | None, dedupe_key: str, new_status: str
) -> JSONResponse:
    """Voids an unpaid batch and requeues its commitments."""
    batch = ledger.release_batch(payment_link_id=link_id, status=new_status) if link_id else None

    if batch is None:
        ledger.finish_webhook(
            dedupe_key=dedupe_key, outcome="unknown_link", paymentLinkId=link_id
        )
        ledger.log_event(
            "webhook_link_not_matched",
            status="ok",
            razorpayEvent=event,
            paymentLinkId=link_id,
            note=(
                "No voidable batch matches this link — unknown, or already paid. "
                "An expiry notice for a link that was paid is normal and is ignored."
            ),
        )
        return JSONResponse(status_code=200, content={"status": "no_matching_batch"})

    ledger.finish_webhook(
        dedupe_key=dedupe_key, outcome="applied", batchId=batch["batch_id"], status=new_status
    )
    ledger.log_event(
        "batch_voided",
        agent_id=batch["agent_id"],
        amount_paise=batch["total_paise"],
        status="ok",
        batchId=batch["batch_id"],
        paymentLinkId=link_id,
        newStatus=new_status,
        note=(
            "Link will never be paid, but the debt behind it is still real — its "
            "commitments are back to pending and the next batch run will re-bill them."
        ),
    )
    return JSONResponse(
        status_code=200,
        content={"status": "voided", "batchId": batch["batch_id"], "event": event},
    )


def webhook_secret_from_env() -> str:
    """The configured webhook secret, or empty string."""
    return (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()
