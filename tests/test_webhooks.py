"""Razorpay webhook intake.

This is the only endpoint in the service that an unauthenticated stranger can
POST to and have money marked as received, so it gets the most adversarial
tests in the suite. The four properties under test, in order of how badly
getting them wrong would hurt:

  1. An unsigned or wrongly-signed delivery changes nothing.
  2. With no secret configured, *nothing* is accepted — the endpoint fails
     closed rather than degrading to trust-everyone.
  3. A redelivered event is applied exactly once, because Razorpay retries
     until it gets a 2xx and duplicates are routine rather than exotic.
  4. Signature verification is over the raw bytes, so a body that was
     re-serialised in transit fails rather than silently passing.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json

import pytest
from conftest import TEST_SECRET
from ledger import Ledger
from test_full_flow import payment_envelope, quote

WEBHOOK_SECRET = "whsec-test-not-the-real-one"


def signed(body: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    """Serialises a webhook body and signs the exact bytes that will be sent.

    Returns `(raw_bytes, headers)`. The test posts `content=raw` rather than
    `json=body` on purpose: re-serialising would change the bytes and the
    signature would no longer describe what arrived, which is precisely the
    failure mode `test_signature_covers_the_raw_bytes` exists to catch.
    """
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Razorpay-Signature": signature,
        "Content-Type": "application/json",
    }


def paid_event(link_id: str, *, amount_paid: int, payment_id: str = "pay_TESTPAYMENT01") -> dict:
    """Razorpay's `payment_link.paid` payload shape."""
    return {
        "event": "payment_link.paid",
        "entity": "event",
        "account_id": "acc_TEST",
        "created_at": 1786500000,
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "reference_id": "batch_test",
                    "amount": amount_paid,
                    "amount_paid": amount_paid,
                    "status": "paid",
                }
            },
            "payment": {"entity": {"id": payment_id, "amount": amount_paid, "status": "captured"}},
        },
    }


def link_event(link_id: str, event: str) -> dict:
    """A `payment_link.expired` / `.cancelled` payload."""
    status = event.split(".")[-1]
    return {
        "event": event,
        "entity": "event",
        "created_at": 1786500000,
        "payload": {
            "payment_link": {
                "entity": {"id": link_id, "amount": 1000, "amount_paid": 0, "status": status}
            }
        },
    }


@pytest.fixture
def hooked(ledger_path, monkeypatch):
    """A facilitator with webhooks configured and one settled batch waiting.

    Yields `(client, link_id, agent_id, ledger)` — a real batch created
    through the real settlement path, so the webhook has something genuine to
    act on rather than a hand-inserted row.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("MOCK_RAZORPAY", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)

    import main

    importlib.reload(main)

    agent_id = "agent-webhook"
    with TestClient(main.app) as client:
        envelope = payment_envelope(quote(client, agent_id=agent_id), agent_id=agent_id)
        client.post("/settle", json=envelope)
        batch = client.post("/settle-batch", json={"agentId": agent_id}).json()
        link_id = batch["batches"][0]["paymentLinkId"]
        yield client, link_id, agent_id, Ledger(str(ledger_path))


class TestWebhookAuthentication:
    def test_unconfigured_endpoint_refuses_everything(self, ledger_path, monkeypatch):
        """No secret means no way to tell Razorpay from anyone else. 503 and
        not a single write — the tempting 'skip verification in dev' default
        is how an unauthenticated ledger-write endpoint reaches production."""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
        monkeypatch.setenv("MOCK_RAZORPAY", "true")
        monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
        monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)

        import main

        importlib.reload(main)
        with TestClient(main.app) as client:
            raw, headers = signed(paid_event("plink_X", amount_paid=500))
            response = client.post("/webhooks/razorpay", content=raw, headers=headers)
            assert response.status_code == 503
            assert response.json()["error"] == "webhooks_not_configured"

    def test_missing_signature_is_rejected(self, hooked):
        client, link_id, _, ledger = hooked
        raw, _ = signed(paid_event(link_id, amount_paid=500))
        response = client.post(
            "/webhooks/razorpay", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert ledger.get_batch_by_link(link_id)["status"] == "created"

    def test_wrong_signature_is_rejected_and_changes_nothing(self, hooked):
        client, link_id, _, ledger = hooked
        raw, headers = signed(paid_event(link_id, amount_paid=500), secret="attacker-guess")
        response = client.post("/webhooks/razorpay", content=raw, headers=headers)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_signature"
        batch = ledger.get_batch_by_link(link_id)
        assert batch["status"] == "created"
        assert batch["paid_at"] is None

    def test_signature_covers_the_raw_bytes(self, hooked):
        """Sign one body, send a different one. This is the check that fails
        if verification is done over a re-serialised parse instead of the
        bytes that actually arrived."""
        client, link_id, _, ledger = hooked
        _, headers = signed(paid_event(link_id, amount_paid=500))
        tampered = json.dumps(paid_event(link_id, amount_paid=999_999)).encode()

        response = client.post("/webhooks/razorpay", content=tampered, headers=headers)
        assert response.status_code == 400
        assert ledger.get_batch_by_link(link_id)["status"] == "created"

    def test_valid_signature_over_non_json_is_rejected(self, hooked):
        """Signed by Razorpay but unparseable. Rejected after the signature
        check, never before — parsing attacker-controlled bytes first is how
        a parser bug becomes a pre-auth vulnerability."""
        client, _, _, _ = hooked
        raw = b"this is signed but is not json"
        signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/razorpay", content=raw, headers={"X-Razorpay-Signature": signature}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_body"


class TestWebhookPayment:
    def test_paid_event_marks_the_batch_collected(self, hooked):
        client, link_id, _, ledger = hooked
        raw, headers = signed(paid_event(link_id, amount_paid=500))

        response = client.post("/webhooks/razorpay", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "applied"

        batch = ledger.get_batch_by_link(link_id)
        assert batch["status"] == "paid"
        assert batch["amount_paid_paise"] == 500
        assert batch["razorpay_payment_id"] == "pay_TESTPAYMENT01"
        assert batch["paid_at"] is not None

    def test_committed_and_collected_are_reported_separately(self, hooked):
        """Before the webhook the publisher has billed ₹5 and collected ₹0.
        Reporting one number for both is how a ledger overstates revenue."""
        client, link_id, agent_id, ledger = hooked

        before = ledger.daily_summary(agent_id=agent_id)
        assert before["committedPaise"] == 500
        assert before["collectedPaise"] == 0
        assert before["paidBatches"] == 0

        raw, headers = signed(paid_event(link_id, amount_paid=500))
        client.post("/webhooks/razorpay", content=raw, headers=headers)

        after = ledger.daily_summary(agent_id=agent_id)
        assert after["committedPaise"] == 500
        assert after["collectedPaise"] == 500
        assert after["paidBatches"] == 1

    def test_redelivery_is_applied_exactly_once(self, hooked):
        """Razorpay retries until it gets a 2xx, so this is the normal case,
        not an edge case. Applying twice would double-count collected revenue.
        """
        client, link_id, agent_id, ledger = hooked
        raw, headers = signed(paid_event(link_id, amount_paid=500))
        headers["X-Razorpay-Event-Id"] = "evt_retry_me"

        first = client.post("/webhooks/razorpay", content=raw, headers=headers)
        second = client.post("/webhooks/razorpay", content=raw, headers=headers)
        third = client.post("/webhooks/razorpay", content=raw, headers=headers)

        assert first.json()["status"] == "applied"
        # 200 on the retries is what stops Razorpay retrying forever.
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert third.json()["status"] == "duplicate"

        assert ledger.daily_summary(agent_id=agent_id)["collectedPaise"] == 500

    def test_unknown_link_is_acknowledged_not_errored(self, hooked):
        """A 404 would make Razorpay retry a delivery we have correctly
        decided to ignore — another service on the same Razorpay account is
        the ordinary explanation."""
        client, _, _, _ = hooked
        raw, headers = signed(paid_event("plink_SOMEONE_ELSES", amount_paid=500))

        response = client.post("/webhooks/razorpay", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "no_matching_batch"

    def test_unhandled_event_is_acknowledged_and_recorded(self, hooked):
        client, link_id, _, ledger = hooked
        raw, headers = signed(
            {"event": "payment.authorized", "payload": {}, "created_at": 1786500000}
        )

        response = client.post("/webhooks/razorpay", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert ledger.get_batch_by_link(link_id)["status"] == "created"

    def test_short_payment_is_recorded_rather_than_rounded_up(self, hooked):
        """Billed ₹5, received ₹3. The ledger must show what arrived, not
        what was asked for."""
        client, link_id, agent_id, ledger = hooked
        raw, headers = signed(paid_event(link_id, amount_paid=300))
        client.post("/webhooks/razorpay", content=raw, headers=headers)

        assert ledger.get_batch_by_link(link_id)["amount_paid_paise"] == 300
        summary = ledger.daily_summary(agent_id=agent_id)
        assert summary["committedPaise"] == 500
        assert summary["collectedPaise"] == 300


class TestWebhookVoiding:
    @pytest.mark.parametrize("event", ["payment_link.expired", "payment_link.cancelled"])
    def test_void_returns_commitments_to_the_queue(self, hooked, event):
        """The link will never be paid, but the debt behind it is still real —
        so the next settlement run has to see it again."""
        client, link_id, agent_id, ledger = hooked
        assert ledger.pending_commitments(agent_id=agent_id) == []

        raw, headers = signed(link_event(link_id, event))
        response = client.post("/webhooks/razorpay", content=raw, headers=headers)

        assert response.json()["status"] == "voided"
        assert ledger.get_batch_by_link(link_id)["status"] == event.split(".")[-1]

        requeued = ledger.pending_commitments(agent_id=agent_id)
        assert len(requeued) == 1
        assert requeued[0]["batch_id"] is None

    def test_requeued_commitments_are_rebilled_by_the_next_run(self, hooked):
        """End to end: expiry, then a fresh settlement run that picks the debt
        back up on a new link."""
        client, link_id, agent_id, ledger = hooked
        raw, headers = signed(link_event(link_id, "payment_link.expired"))
        client.post("/webhooks/razorpay", content=raw, headers=headers)

        rerun = client.post("/settle-batch", json={"agentId": agent_id}).json()
        assert rerun["batchCount"] == 1
        assert rerun["batches"][0]["totalPaise"] == 500
        assert rerun["batches"][0]["paymentLinkId"] != link_id

    def test_expiry_after_payment_does_not_uncollect_money(self, hooked):
        """Razorpay expires a link on schedule whether or not it was paid, so
        an `expired` arriving after a `paid` is normal. Acting on it would
        reverse a genuine collection."""
        client, link_id, agent_id, ledger = hooked

        raw, headers = signed(paid_event(link_id, amount_paid=500))
        client.post("/webhooks/razorpay", content=raw, headers=headers)

        raw, headers = signed(link_event(link_id, "payment_link.expired"))
        response = client.post("/webhooks/razorpay", content=raw, headers=headers)

        assert response.status_code == 200
        assert response.json()["status"] == "no_matching_batch"
        assert ledger.get_batch_by_link(link_id)["status"] == "paid"
        assert ledger.daily_summary(agent_id=agent_id)["collectedPaise"] == 500
        assert ledger.pending_commitments(agent_id=agent_id) == []


class TestWebhookSignatureHelper:
    """The verification primitive on its own, independent of FastAPI."""

    def test_accepts_a_correct_signature(self):
        from webhooks import verify_webhook_signature

        raw = b'{"event":"payment_link.paid"}'
        signature = hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(raw_body=raw, signature=signature, secret="s3cret")

    def test_rejects_a_signature_for_different_bytes(self):
        from webhooks import verify_webhook_signature

        signature = hmac.new(b"s3cret", b"original", hashlib.sha256).hexdigest()
        assert not verify_webhook_signature(
            raw_body=b"modified", signature=signature, secret="s3cret"
        )

    def test_rejects_empty_and_none_signatures(self):
        from webhooks import verify_webhook_signature

        raw = b'{"event":"x"}'
        assert not verify_webhook_signature(raw_body=raw, signature="", secret="s3cret")
        assert not verify_webhook_signature(raw_body=raw, signature=None, secret="s3cret")
