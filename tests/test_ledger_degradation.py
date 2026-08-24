"""How the facilitator behaves when its ledger is unreachable.

The bug these cover, found by watching production during a Supabase outage: a
webhook with a bad signature — which should be a fast, cheap 400 — instead
spent ten seconds blocked on the audit insert's connection timeout, raised,
hit the unhandled-error handler, which logged *again* for another ten seconds,
and returned a 500. Razorpay's answer to a slow 5xx is to retry, so an
outage generated extra load on the way down.

The fix has three parts and each is pinned below:

  1. `log_event` writes stdout before the database, so a dead ledger loses the
     row but not the event.
  2. `log_event` never raises.
  3. After one failure it stops attempting inserts for a short window, so a
     request that logs several events does not pay the timeout each time.

And the boundary that matters more than any of them: **money writes still
raise.** An unrecorded log line is recoverable from stdout; an unrecorded
commitment is revenue that silently never existed.
"""

from __future__ import annotations

import json

import pytest
from conftest import TEST_SECRET
from ledger import Ledger


class BrokenConnection(Exception):
    """Stands in for psycopg's PoolTimeout / OperationalError."""


@pytest.fixture
def broken_ledger(ledger_path, monkeypatch):
    """A ledger whose schema exists but whose connections now all fail."""
    built = Ledger(str(ledger_path))

    def refuse(*_args, **_kwargs):
        raise BrokenConnection(
            "PoolTimeout: couldn't get a connection after 10.00 sec "
            "(postgresql://user:hunter2@db.example.com:6543/postgres)"
        )

    monkeypatch.setattr(built, "_connect", refuse)
    return built


class TestAuditLogDegrades:
    def test_log_event_does_not_raise_when_the_ledger_is_down(self, broken_ledger):
        broken_ledger.log_event("webhook_signature_invalid", status="rejected")

    def test_the_event_still_reaches_stdout(self, broken_ledger, capsys):
        """Ordered stdout-first on purpose: the previous order lost the row
        *and* the log line, which is the worst of both."""
        broken_ledger.log_event("payment_verify_rejected", agent_id="agent-x", status="rejected")

        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        events = [line["event"] for line in lines]
        assert "payment_verify_rejected" in events

    def test_the_failure_is_reported_rather_than_swallowed(self, broken_ledger, capsys):
        """A silently degraded audit trail is how you discover months later
        that you have no audit trail."""
        broken_ledger.log_event("something_notable", status="ok")

        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        failures = [line for line in lines if line["event"] == "ledger_write_failed"]
        assert failures, "a dropped audit row must announce itself"
        assert failures[0]["droppedEvent"] == "something_notable"
        assert failures[0]["status"] == "degraded"

    def test_the_failure_report_does_not_leak_the_dsn(self, broken_ledger, capsys):
        """Driver errors quote the connection string they failed on, and this
        one goes to stdout."""
        broken_ledger.log_event("something_notable", status="ok")
        assert "hunter2" not in capsys.readouterr().out

    def test_repeated_failures_stop_hammering_the_database(self, broken_ledger, capsys):
        """The latency fix. Without the breaker, ten logged events during an
        outage is ten connection timeouts."""
        attempts = 0
        original = broken_ledger._connect

        def counting(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return original(*args, **kwargs)

        broken_ledger._connect = counting

        for i in range(10):
            broken_ledger.log_event(f"event_{i}", status="ok")

        assert attempts == 1, f"expected one attempt then suppression, got {attempts}"

        # Every event is still on stdout — suppressed persistence, not
        # suppressed logging.
        out = capsys.readouterr().out
        for i in range(10):
            assert f"event_{i}" in out

    def test_the_breaker_reopens_once_the_window_lapses(self, broken_ledger, monkeypatch):
        import ledger as ledger_module

        monkeypatch.setattr(ledger_module, "AUDIT_BREAKER_SECONDS", 0.0)

        attempts = 0
        original = broken_ledger._connect

        def counting(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return original(*args, **kwargs)

        broken_ledger._connect = counting

        broken_ledger.log_event("first", status="ok")
        broken_ledger.log_event("second", status="ok")
        assert attempts == 2, "a zero-length window should not suppress anything"

    def test_a_healthy_ledger_still_persists_events(self, ledger):
        """The degradation path must not cost the normal one."""
        ledger.log_event("payment_verified", agent_id="agent-ok", status="ok")
        events = ledger.list_events(agent_id="agent-ok")
        assert [e["event"] for e in events] == ["payment_verified"]

    def test_a_recovered_ledger_resumes_writing(self, ledger_path, monkeypatch, capsys):
        """One failure must not permanently disable the audit trail."""
        built = Ledger(str(ledger_path))
        working = built._connect

        monkeypatch.setattr(built, "_connect", lambda *a, **k: (_ for _ in ()).throw(
            BrokenConnection("down")
        ))
        built.log_event("during_outage", status="ok")

        # Recovery, and a lapsed window.
        monkeypatch.setattr(built, "_connect", working)
        built._audit_breaker_open_until = 0.0
        built.log_event("after_recovery", agent_id="agent-back", status="ok")

        assert [e["event"] for e in built.list_events(agent_id="agent-back")] == ["after_recovery"]


class TestMoneyWritesStillFail:
    """The boundary. Leniency is scoped to the audit log."""

    def test_create_commitment_raises_when_the_ledger_is_down(self, broken_ledger):
        with pytest.raises(BrokenConnection):
            broken_ledger.create_commitment(
                commitment_id="cmt_x",
                offer_id="off_x",
                agent_id="agent-x",
                resource_id="r",
                amount_paise=500,
                asset="INR",
                mode="deferred",
            )

    def test_record_batch_raises_when_the_ledger_is_down(self, broken_ledger):
        with pytest.raises(BrokenConnection):
            broken_ledger.record_batch(
                batch_id="batch_x",
                agent_id="agent-x",
                settle_date="2026-08-22",
                commitment_ids=["cmt_x"],
                total_paise=500,
                payment_link_id="plink_x",
                payment_link_url=None,
                status="created",
                razorpay_mode="mock",
            )

    def test_mark_batch_paid_raises_when_the_ledger_is_down(self, broken_ledger):
        with pytest.raises(BrokenConnection):
            broken_ledger.mark_batch_paid(
                payment_link_id="plink_x", amount_paid_paise=500, razorpay_payment_id="pay_x"
            )


class TestWebhookStaysFastWhenTheLedgerIsDown:
    """The original symptom, end to end."""

    def test_bad_signature_is_still_a_prompt_400(self, ledger_path, monkeypatch):
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
        monkeypatch.setenv("MOCK_RAZORPAY", "true")
        monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec-test")

        import main

        importlib.reload(main)

        attempts = 0
        original = main.ledger._connect

        def counting(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise BrokenConnection("PoolTimeout: couldn't get a connection")

        monkeypatch.setattr(main.ledger, "_connect", counting)

        with TestClient(main.app) as client:
            response = client.post(
                "/webhooks/razorpay",
                content=b'{"event":"payment_link.paid"}',
                headers={"X-Razorpay-Signature": "wrong", "Content-Type": "application/json"},
            )

        # A 400, not a 500 that Razorpay would retry.
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_signature"
        # And one connection attempt, not one per logged event.
        assert attempts <= 1, f"the dead ledger was dialled {attempts} times"

        assert original is not None  # sanity: the fixture really had a connector
