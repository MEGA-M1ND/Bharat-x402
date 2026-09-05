"""End-to-end and unit coverage for the Bharat x402 payment flow.

Organised around the four things that must be true for this project to be
worth anything:

    1. An unpaid request is refused with a well-formed INR offer.
    2. A valid payment unlocks the resource.
    3. A tampered or replayed payment does not, and the reason is recorded.
    4. Batch settlement totals reconcile exactly against the ledger.

Money assertions are on exact integer paise. A payment system that is
approximately right is wrong.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import TEST_SECRET
from demo_trace import _build_agent_id
from ledger import Ledger
from payment_verifier import (
    OfferPolicy,
    VerificationError,
    agent_commitment_body,
    build_offer,
    canonical_json,
    sign,
    verify_payment_proof,
    verify_signature,
)
from razorpay_client import (
    FeeModel,
    RazorpayConfigError,
    RazorpayGateway,
    estimate_settlement_cost,
    format_paise,
)

PAY_TO = "acc_BharatNewsNetwork"
RESOURCE_ID = "market-report-2026-08"
PRICE_PAISE = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hmac_sign(body: dict, secret: str = TEST_SECRET) -> str:
    """Signs the way an agent would, independently of the facilitator's code.

    Deliberately not calling `payment_verifier.sign` here: a test that reuses
    the implementation it is checking will happily pass even if both sides
    agree on the wrong thing.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def quote(client, *, agent_id: str, amount_paise: int = PRICE_PAISE) -> dict:
    """Asks the facilitator for an offer."""
    response = client.post(
        "/offer",
        json={
            "agentId": agent_id,
            "resourceId": RESOURCE_ID,
            "amountPaise": amount_paise,
            "payTo": PAY_TO,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def payment_envelope(quoted: dict, *, agent_id: str, secret: str = TEST_SECRET) -> dict:
    """Builds the `{x402Version, paymentPayload, paymentRequirements}` body."""
    offer = quoted["offer"]
    accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    commitment = dict(quoted["commitmentTemplate"])
    commitment["acceptedAt"] = accepted_at

    requirements = {
        "scheme": offer["scheme"],
        "network": offer["network"],
        "asset": offer["asset"],
        "amount": str(offer["amountPaise"]),
        "payTo": offer["payTo"],
        "maxTimeoutSeconds": 300,
        "extra": {},
    }

    return {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "accepted": requirements,
            "payload": {
                "offerId": offer["offerId"],
                "agentId": agent_id,
                "acceptedAt": accepted_at,
                "agentSignature": hmac_sign(commitment, secret),
            },
        },
        "paymentRequirements": requirements,
    }


# ---------------------------------------------------------------------------
# 1. Signing and verification
# ---------------------------------------------------------------------------


class TestSignatures:
    def test_canonical_json_ignores_key_order(self):
        """The same logical object must always sign the same bytes."""
        assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})

    def test_canonical_json_has_no_incidental_whitespace(self):
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_signature_round_trip(self):
        body = {"offerId": "off_x", "amountPaise": 500}
        assert verify_signature(body, sign(body, "secret"), "secret")

    def test_signature_rejects_wrong_secret(self):
        body = {"offerId": "off_x"}
        assert not verify_signature(body, sign(body, "secret"), "other-secret")

    def test_signature_rejects_mutated_body(self):
        body = {"offerId": "off_x", "amountPaise": 500}
        signature = sign(body, "secret")
        assert not verify_signature({**body, "amountPaise": 1}, signature, "secret")

    def test_signature_rejects_empty(self):
        """A missing signature must not be treated as a matching one."""
        assert not verify_signature({"a": 1}, "", "secret")
        assert not verify_signature({"a": 1}, None, "secret")


class TestOfferPolicy:
    def test_rejects_zero_amount(self):
        with pytest.raises(VerificationError) as exc:
            build_offer(
                agent_id="a", resource_id="r", amount_paise=0,
                pay_to=PAY_TO, policy=OfferPolicy(),
            )
        assert exc.value.reason == "invalid_amount"

    def test_rejects_negative_amount(self):
        with pytest.raises(VerificationError):
            build_offer(
                agent_id="a", resource_id="r", amount_paise=-500,
                pay_to=PAY_TO, policy=OfferPolicy(),
            )

    def test_rejects_amount_over_ceiling(self):
        """Guards against a misconfigured publisher billing ₹10,00,000 a call."""
        with pytest.raises(VerificationError) as exc:
            build_offer(
                agent_id="a", resource_id="r", amount_paise=10_000_000,
                pay_to=PAY_TO, policy=OfferPolicy(max_amount_paise=100_000),
            )
        assert exc.value.reason == "amount_too_large"

    def test_rejects_missing_agent(self):
        with pytest.raises(VerificationError) as exc:
            build_offer(
                agent_id="", resource_id="r", amount_paise=500,
                pay_to=PAY_TO, policy=OfferPolicy(),
            )
        assert exc.value.reason == "missing_agent_id"

    def test_offers_are_unique(self):
        """Identical parameters must still produce distinct, unlinkable offers."""
        kwargs = dict(
            agent_id="a", resource_id="r", amount_paise=500,
            pay_to=PAY_TO, policy=OfferPolicy(),
        )
        first, second = build_offer(**kwargs), build_offer(**kwargs)
        assert first["offerId"] != second["offerId"]
        assert first["nonce"] != second["nonce"]


# ---------------------------------------------------------------------------
# 2. The ledger
# ---------------------------------------------------------------------------


class TestDbShim:
    """facilitator/db.py — the SQLite/Postgres dialect translation itself."""

    def test_postgres_placeholders_translate(self):
        from db import _to_postgres_placeholders

        assert _to_postgres_placeholders("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_split_sql_statements_does_not_break_on_a_comment_containing_a_semicolon(self):
        """Regression test for a real CI failure, not a hypothetical one.

        A DDL comment that happens to contain an English semicolon —
        "...ever updated or deleted; it is the audit trail..." — used to
        split the script mid-sentence. Postgres was then handed a fragment
        starting with "it is the", with no `--` in front of it, which is a
        syntax error rather than a comment. Caught by the real postgres:16
        CI job, not by any local reasoning about the splitter.
        """
        from db import _split_sql_statements

        script = (
            "-- a comment; with a semicolon in it\n"
            "CREATE TABLE a (x INT);\n"
            "-- another; one; with several\n"
            "CREATE TABLE b (y INT);"
        )
        statements = _split_sql_statements(script)
        assert statements == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]

    def test_split_sql_statements_against_the_real_schema(self):
        """The actual schema, not a synthetic example: every statement is a
        whole DDL statement and none is a fragment of a comment.

        Counts CREATEs in the source rather than asserting a fixed number —
        the bug this guards against is the splitter producing *fragments*,
        which a literal count would flag every time the schema legitimately
        gains a table, training everyone to update the number without
        looking at what actually broke.
        """
        from db import _split_sql_statements
        from ledger import schema_sql

        schema = schema_sql("postgres")
        statements = _split_sql_statements(schema)

        assert len(statements) == schema.upper().count("CREATE TABLE") + schema.upper().count(
            "CREATE INDEX"
        )
        for statement in statements:
            assert statement.upper().startswith(("CREATE TABLE", "CREATE INDEX")), statement

    def test_dialect_for(self):
        from db import dialect_for

        assert dialect_for("postgres://u:p@host/db") == "postgres"
        assert dialect_for("postgresql://u:p@host/db") == "postgres"
        assert dialect_for("./data/ledger.db") == "sqlite"
        assert dialect_for("/absolute/path/ledger.db") == "sqlite"


class TestLedger:
    def _store_offer(self, ledger: Ledger, agent_id: str, amount: int = PRICE_PAISE) -> dict:
        offer = build_offer(
            agent_id=agent_id,
            resource_id=RESOURCE_ID,
            amount_paise=amount,
            pay_to=PAY_TO,
            policy=OfferPolicy(),
        )
        ledger.insert_offer(offer, sign(offer, TEST_SECRET))
        return offer

    def test_commitment_consumes_its_offer(self, ledger):
        offer = self._store_offer(ledger, "agent-a")
        assert ledger.get_offer(offer["offerId"])["status"] == "open"

        ledger.create_commitment(
            commitment_id="cmt_1", offer_id=offer["offerId"], agent_id="agent-a",
            resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
        )
        assert ledger.get_offer(offer["offerId"])["status"] == "consumed"

    def test_offer_cannot_be_spent_twice(self, ledger):
        """The core double-spend guard, enforced by the database."""
        offer = self._store_offer(ledger, "agent-a")
        ledger.create_commitment(
            commitment_id="cmt_1", offer_id=offer["offerId"], agent_id="agent-a",
            resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
        )
        with pytest.raises(ValueError, match="already consumed"):
            ledger.create_commitment(
                commitment_id="cmt_2", offer_id=offer["offerId"], agent_id="agent-a",
                resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
            )

    def test_unknown_offer_cannot_be_committed(self, ledger):
        with pytest.raises(ValueError, match="does not exist"):
            ledger.create_commitment(
                commitment_id="cmt_1", offer_id="off_nope", agent_id="a",
                resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
            )

    def test_revenue_below_counts_individual_charges(self, ledger):
        """Mixed traffic: the figure must come from rows, not from averages.

        This is the bug that shipped once. One ₹5 fetch plus four ₹0.50 calls
        has a mean above the ₹1 Payment Links floor while four of five charges
        fall below it.
        """
        amounts = [500, 50, 50, 50, 50]
        for index, amount in enumerate(amounts):
            offer = self._store_offer(ledger, "agent-mixed", amount)
            ledger.create_commitment(
                commitment_id=f"cmt_{index}", offer_id=offer["offerId"],
                agent_id="agent-mixed", resource_id=RESOURCE_ID,
                amount_paise=amount, asset="INR", mode="deferred",
            )

        below = ledger.revenue_below(100)
        assert below["count"] == 4
        assert below["totalPaise"] == 200  # not 0, which an average would give

    def test_daily_summary_reconciles(self, ledger):
        for index in range(5):
            offer = self._store_offer(ledger, f"agent-{index % 2}")
            ledger.create_commitment(
                commitment_id=f"cmt_{index}", offer_id=offer["offerId"],
                agent_id=f"agent-{index % 2}", resource_id=RESOURCE_ID,
                amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
            )

        summary = ledger.daily_summary()
        assert summary["requests"] == 5
        assert summary["totalPaise"] == 5 * PRICE_PAISE
        assert sum(a["total_paise"] for a in summary["byAgent"]) == summary["totalPaise"]

    def test_failed_batch_leaves_commitments_pending(self, ledger):
        """A charge that failed must never look like one that succeeded."""
        offer = self._store_offer(ledger, "agent-a")
        ledger.create_commitment(
            commitment_id="cmt_1", offer_id=offer["offerId"], agent_id="agent-a",
            resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE, asset="INR", mode="deferred",
        )
        ledger.record_batch(
            batch_id="batch_1", agent_id="agent-a",
            settle_date=ledger.daily_summary()["settleDate"],
            commitment_ids=["cmt_1"], total_paise=PRICE_PAISE, payment_link_id=None,
            payment_link_url=None, status="failed", razorpay_mode="mock",
            error_message="gateway exploded",
        )
        assert len(ledger.pending_commitments(agent_id="agent-a")) == 1


# ---------------------------------------------------------------------------
# 3. Settlement economics
# ---------------------------------------------------------------------------


class TestFeeModel:
    def test_fee_is_percentage_plus_gst(self):
        model = FeeModel(percent_bps=200, fixed_paise=0, gst_bps=1800)
        # 2% of ₹100 is ₹2; 18% GST on ₹2 is ₹0.36; total ₹2.36.
        assert model.fee_for(10_000) == 236

    def test_fixed_component_is_added(self):
        model = FeeModel(percent_bps=0, fixed_paise=300, gst_bps=0)
        assert model.fee_for(10_000) == 300

    def test_percentage_fees_are_neutral_to_batching(self):
        """The claim the README refuses to make, asserted so it stays refused."""
        model = FeeModel(percent_bps=200, fixed_paise=0, gst_bps=0)
        many = estimate_settlement_cost([10_000] * 10, model)
        assert many["perRequestFeePaise"] == many["batchedFeePaise"]
        assert many["comparableFeeDeltaPaise"] == 0

    def test_fixed_fees_are_not_neutral_to_batching(self):
        """Where batching genuinely saves money on fees."""
        model = FeeModel(percent_bps=0, fixed_paise=300, gst_bps=0)
        result = estimate_settlement_cost([10_000] * 10, model)
        assert result["perRequestFeePaise"] == 3000
        assert result["batchedFeePaise"] == 300

    def test_sub_minimum_revenue_is_reported_as_unreachable(self):
        """The actual argument: these charges have no per-request path at all."""
        model = FeeModel(minimum_charge_paise=100)
        result = estimate_settlement_cost([50] * 400, model)
        assert result["belowGatewayMinimum"] == 400
        assert result["revenueUnreachablePerRequestPaise"] == 20_000
        assert result["gatewayCallsSaved"] == 399
        # No fee is quoted for charges that could not have been made.
        assert result["chargeableCount"] == 0
        assert result["perRequestFeePaise"] == 0

    def test_format_paise(self):
        assert format_paise(500) == "₹5.00"
        assert format_paise(50) == "₹0.50"
        assert format_paise(0) == "₹0.00"
        assert format_paise(123_456) == "₹1234.56"


class TestRazorpayGuards:
    def test_live_key_is_refused(self):
        """A demo that creates payment links in a loop must not hold a live key."""
        with pytest.raises(RazorpayConfigError, match="live"):
            RazorpayGateway(key_id="rzp_live_abc123", key_secret="secret", mock=False)

    def test_live_key_refused_even_in_mock_mode(self):
        """The guard must not be bypassable by asking for mock mode."""
        with pytest.raises(RazorpayConfigError):
            RazorpayGateway(key_id="rzp_live_abc123", key_secret="secret", mock=True)

    def test_missing_credentials_fall_back_to_mock(self):
        assert RazorpayGateway(key_id="", key_secret="").mock is True

    def test_mock_mode_needs_no_credentials(self):
        ok, detail = RazorpayGateway(key_id="", key_secret="").check_credentials()
        assert ok is True
        assert "mock" in detail

    def test_bad_credentials_are_caught_at_startup(self, monkeypatch):
        """Broken keys must surface on boot, not at the end of the first day.

        In deferred mode nothing touches Razorpay until settlement runs, so
        without this check a facilitator comes up looking healthy and only
        discovers its credentials are dead once a day of commitments is
        already booked behind them.
        """
        gateway = RazorpayGateway(key_id="rzp_test_fake", key_secret="fake", mock=False)

        class RejectingLinks:
            @staticmethod
            def all(*args, **kwargs):
                raise Exception("Authentication failed")

        monkeypatch.setattr(gateway, "_client", type("C", (), {"payment_link": RejectingLinks})())

        ok, detail = gateway.check_credentials()
        assert ok is False
        assert "same key pair" in detail

    def test_credential_failure_does_not_fall_back_to_mock(self, monkeypatch):
        """A gateway that cannot authenticate must not quietly fake payments.

        Degrading to mock on an auth error would report settled revenue that
        does not exist — worse than failing loudly.
        """
        gateway = RazorpayGateway(key_id="rzp_test_fake", key_secret="fake", mock=False)

        class RejectingLinks:
            @staticmethod
            def all(*args, **kwargs):
                raise Exception("Authentication failed")

        monkeypatch.setattr(gateway, "_client", type("C", (), {"payment_link": RejectingLinks})())
        gateway.check_credentials()

        assert gateway.mock is False
        assert gateway.mode == "razorpay_test"

    def test_rate_limits_are_retried(self, monkeypatch):
        """Razorpay rate-limits, and settlement creates links back to back.

        Found by running against the real test API: a publisher with enough
        paying agents hits 429 partway through a settlement run.
        """
        gateway = RazorpayGateway(key_id="rzp_test_fake", key_secret="fake", mock=False)
        calls = {"n": 0}

        class FlakyLinks:
            @staticmethod
            def create(payload):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise Exception("Too many requests")
                return {"id": "plink_ok", "short_url": "https://rzp.io/x", "status": "created"}

        monkeypatch.setattr(gateway, "_client", type("C", (), {"payment_link": FlakyLinks})())
        monkeypatch.setattr("razorpay_client.time.sleep", lambda _: None)

        link = gateway.create_payment_link(
            amount_paise=500, description="d", reference_id="r", agent_id="a"
        )
        assert link["id"] == "plink_ok"
        assert calls["n"] == 3

    def test_non_rate_limit_errors_are_not_retried(self, monkeypatch):
        """A rejected amount fails identically every time; retrying just delays the report."""
        gateway = RazorpayGateway(key_id="rzp_test_fake", key_secret="fake", mock=False)
        calls = {"n": 0}

        class RejectingLinks:
            @staticmethod
            def create(payload):
                calls["n"] += 1
                raise Exception("amount: amount should be minimum 1.00 for INR.")

        monkeypatch.setattr(gateway, "_client", type("C", (), {"payment_link": RejectingLinks})())

        with pytest.raises(Exception, match="minimum 1.00"):
            gateway.create_payment_link(
                amount_paise=500, description="d", reference_id="r", agent_id="a"
            )
        assert calls["n"] == 1

    def test_below_minimum_charge_is_refused(self):
        """The constraint the whole project is built around."""
        gateway = RazorpayGateway(key_id="", key_secret="")
        with pytest.raises(RazorpayConfigError, match="minimum"):
            gateway.create_payment_link(
                amount_paise=50, description="one call",
                reference_id="ref_1", agent_id="agent-a",
            )


# ---------------------------------------------------------------------------
# 4. The facilitator's HTTP contract
# ---------------------------------------------------------------------------


class TestFacilitatorContract:
    def test_supported_advertises_the_inr_scheme(self, client):
        """What makes the stock x402 middleware willing to price an INR route."""
        body = client.get("/supported").json()
        kind = body["kinds"][0]
        assert kind["x402Version"] == 2
        assert kind["scheme"] == "razorpay-inr"
        assert kind["network"] == "razorpay:inr-test"
        assert kind["extra"]["currency"] == "INR"
        assert kind["extra"]["decimals"] == 2

    def test_offer_is_signed_and_bounded(self, client):
        quoted = quote(client, agent_id="agent-a")
        offer = quoted["offer"]

        assert offer["amountPaise"] == PRICE_PAISE
        assert quoted["humanAmount"] == "₹5.00"
        assert verify_signature(offer, quoted["signature"], TEST_SECRET)
        assert offer["agentId"] == "agent-a"

        expires = datetime.fromisoformat(offer["expiresAt"].replace("Z", "+00:00"))
        assert expires > datetime.now(UTC)
        assert expires < datetime.now(UTC) + timedelta(seconds=400)

    def test_offer_rejects_unsupported_scheme(self, client):
        response = client.post(
            "/offer",
            json={
                "agentId": "agent-a", "resourceId": RESOURCE_ID,
                "amountPaise": PRICE_PAISE, "payTo": PAY_TO,
                "scheme": "exact", "network": "eip155:8453",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "unsupported_kind"

    def test_verify_accepts_a_good_proof(self, client):
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is True
        assert body["payer"] == "agent-a"

    def test_verify_does_not_consume_the_offer(self, client, ledger_path):
        """Verification runs before the handler and may be retried; it must be safe."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        for _ in range(3):
            assert client.post("/verify", json=envelope).json()["isValid"] is True

        offer_id = envelope["paymentPayload"]["payload"]["offerId"]
        assert Ledger(str(ledger_path)).get_offer(offer_id)["status"] == "open"

    @pytest.mark.parametrize(
        ("mutate", "expected_reason"),
        [
            (lambda e: e["paymentPayload"]["payload"].update(agentSignature="0" * 64),
             "invalid_signature"),
            (lambda e: e["paymentPayload"]["payload"].update(offerId="off_nonexistent"),
             "unknown_offer"),
            (lambda e: e["paymentPayload"]["payload"].pop("agentSignature"),
             "malformed_payload"),
            (lambda e: e["paymentPayload"]["payload"].pop("offerId"),
             "malformed_payload"),
            (lambda e: e["paymentRequirements"].update(amount="5000"),
             "amount_mismatch"),
            (lambda e: e["paymentRequirements"].update(payTo="acc_Attacker"),
             "recipient_mismatch"),
            (lambda e: e["paymentRequirements"].update(asset="USDC"),
             "asset_mismatch"),
            (lambda e: e["paymentPayload"]["payload"].update(acceptedAt="2020-01-01T00:00:00Z"),
             "invalid_signature"),
        ],
    )
    def test_verify_rejects_tampering_with_a_specific_reason(
        self, client, mutate, expected_reason
    ):
        """Every rejection names its cause. 'Payment failed' is not a useful log line."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        mutate(envelope)

        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is False
        assert body["invalidReason"] == expected_reason
        assert body["invalidMessage"]

    def test_verify_rejects_a_proof_signed_with_the_wrong_key(self, client):
        envelope = payment_envelope(
            quote(client, agent_id="agent-a"), agent_id="agent-a", secret="not-the-secret"
        )
        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is False
        assert body["invalidReason"] == "invalid_signature"

    def test_rejections_are_written_to_the_audit_log(self, client, ledger_path):
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        envelope["paymentPayload"]["payload"]["agentSignature"] = "0" * 64
        client.post("/verify", json=envelope)

        summary = Ledger(str(ledger_path)).daily_summary()
        assert summary["rejectedPayments"] >= 1


class TestErrorHandling:
    """Nothing fails silently, and nothing leaks internals to the caller."""

    def test_malformed_body_is_rejected_and_logged(self, client, ledger_path):
        response = client.post("/verify", json={"nonsense": True})
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_request"

        assert Ledger(str(ledger_path)).daily_summary()["rejectedPayments"] >= 1

    def test_malformed_offer_request_is_rejected(self, client):
        response = client.post("/offer", json={"agentId": "a"})
        assert response.status_code == 422

    def test_unexpected_errors_are_logged_without_leaking(
        self, crashing_client, facilitator_app, monkeypatch
    ):
        """A ledger blowing up must not surface its internals to an agent."""

        def explode(*args, **kwargs):
            raise RuntimeError("connection string postgres://user:hunter2@db/ledger")

        monkeypatch.setattr(facilitator_app.ledger, "get_offer", explode)

        response = crashing_client.post(
            "/verify",
            json={
                "x402Version": 2,
                "paymentPayload": {"payload": {"offerId": "off_x", "agentSignature": "x",
                                               "acceptedAt": "2026-08-11T00:00:00Z"}},
                "paymentRequirements": {"amount": "500"},
            },
        )

        assert response.status_code == 500
        body = response.text
        assert "hunter2" not in body
        assert "postgres" not in body
        assert response.json()["error"] == "internal_error"

    def test_health_reports_the_razorpay_mode(self, client):
        """Whether real charges can happen is the thing an operator must see."""
        response = client.get("/health")
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "ok"
        assert body["razorpayMode"] == "mock"
        assert body["settlementMode"] == "deferred"
        assert body["ledger"]["reachable"] is True

    def test_health_goes_503_when_the_ledger_is_unreachable(self, client, monkeypatch):
        """The case this endpoint was extended for: the service is up and the
        database is not. Those must not look the same from outside — one is a
        five-minute fix and the other is an afternoon.
        """
        import main

        monkeypatch.setattr(
            main.ledger, "check_connection", lambda: (False, "OperationalError: gone")
        )

        response = client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["ledger"]["reachable"] is False
        assert "OperationalError" in body["ledger"]["detail"]

    def test_redact_credentials_strips_passwords_but_keeps_the_host(self):
        """The host is what makes a connection error diagnosable; the password
        is what must never reach a log line."""
        from ledger import redact_credentials

        redacted = redact_credentials(
            "OperationalError: could not connect to "
            "postgresql://postgres.abc:S3cretPw@db.ap-south-1.example.com:6543/postgres"
        )
        assert "S3cretPw" not in redacted
        assert "postgres.abc" not in redacted
        assert "db.ap-south-1.example.com:6543" in redacted

    def test_redact_credentials_leaves_ordinary_text_alone(self):
        """A scrubber that mangles every message makes errors unreadable and
        gets removed by the next person to debug an outage."""
        from ledger import redact_credentials

        message = "OperationalError: server closed the connection unexpectedly"
        assert redact_credentials(message) == message

    def test_health_never_echoes_the_connection_string(self, client, monkeypatch):
        """A DSN carries a password, and /health is the most casually-shared
        endpoint in any service."""
        import main

        monkeypatch.setattr(
            main.ledger,
            "check_connection",
            lambda: (False, "could not connect to postgres://user:hunter2@db.example.com/x"),
        )

        body = json.dumps(client.get("/health").json())
        assert "hunter2" not in body


class TestSettlement:
    def test_settle_records_a_commitment_without_charging(self, client, ledger_path):
        """Deferred settlement: the protocol is satisfied, no gateway involved."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        body = client.post("/settle", json=envelope).json()

        assert body["success"] is True
        assert body["transaction"].startswith("cmt_")
        assert body["extra"]["settlementMode"] == "deferred"

        ledger = Ledger(str(ledger_path))
        pending = ledger.pending_commitments(agent_id="agent-a")
        assert len(pending) == 1
        assert pending[0]["amount_paise"] == PRICE_PAISE
        # Nothing was charged.
        assert ledger.daily_summary()["batches"] == []

    def test_settle_is_idempotent(self, client, ledger_path):
        """A retried settlement must not create a second debt."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")

        first = client.post("/settle", json=envelope).json()
        second = client.post("/settle", json=envelope).json()

        assert second["success"] is True
        assert second["transaction"] == first["transaction"]
        assert second["extra"]["replayed"] is True
        assert len(Ledger(str(ledger_path)).pending_commitments()) == 1

    def test_settle_rejects_a_tampered_proof(self, client, ledger_path):
        """Settlement re-verifies; it does not trust that /verify ran."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        envelope["paymentPayload"]["payload"]["agentSignature"] = "0" * 64

        body = client.post("/settle", json=envelope).json()
        assert body["success"] is False
        assert body["errorReason"] == "invalid_signature"
        assert body["transaction"] == ""
        assert Ledger(str(ledger_path)).pending_commitments() == []

    def test_batch_produces_one_charge_per_agent(self, client):
        for agent in ("agent-a", "agent-b"):
            for _ in range(3):
                client.post(
                    "/settle", json=payment_envelope(quote(client, agent_id=agent), agent_id=agent)
                )

        result = client.post("/settle-batch", json={}).json()
        batches = result["batches"]

        assert len(batches) == 2
        assert {b["agentId"] for b in batches} == {"agent-a", "agent-b"}
        for batch in batches:
            assert batch["status"] == "created"
            assert batch["commitmentCount"] == 3
            assert batch["totalPaise"] == 3 * PRICE_PAISE
            assert batch["paymentLinkId"].startswith("plink_")

    def test_batch_totals_reconcile_against_the_ledger(self, client, ledger_path):
        """The assertion that matters: no rupee is created or lost by batching."""
        expected = 0
        for agent, fetches in (("agent-a", 4), ("agent-b", 3), ("agent-c", 1)):
            for _ in range(fetches):
                client.post(
                    "/settle", json=payment_envelope(quote(client, agent_id=agent), agent_id=agent)
                )
                expected += PRICE_PAISE

        ledger = Ledger(str(ledger_path))
        assert ledger.daily_summary()["totalPaise"] == expected

        result = client.post("/settle-batch", json={}).json()
        assert sum(b["totalPaise"] for b in result["batches"]) == expected

        summary = ledger.daily_summary()
        settled = sum(b["total_paise"] for b in summary["batches"] if b["status"] == "created")
        assert settled == expected
        assert ledger.pending_commitments() == []

    def test_dry_run_charges_nothing(self, client, ledger_path):
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        client.post("/settle", json=envelope)

        result = client.post("/settle-batch", json={"dryRun": True}).json()
        assert result["batches"][0]["status"] == "dry_run"
        assert result["batches"][0]["paymentLinkId"] is None
        # Still owed afterwards.
        assert len(Ledger(str(ledger_path)).pending_commitments()) == 1

    def test_second_batch_run_is_a_noop(self, client):
        """A scheduler that fires twice must not charge twice."""
        envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
        client.post("/settle", json=envelope)

        assert len(client.post("/settle-batch", json={}).json()["batches"]) == 1
        assert client.post("/settle-batch", json={}).json()["batches"] == []

    def test_batch_with_nothing_pending_is_harmless(self, client):
        result = client.post("/settle-batch", json={}).json()
        assert result["batches"] == []
        assert "No pending commitments" in result["message"]


class TestExpiry:
    def test_expired_offer_is_rejected(self, ledger):
        """Verified directly against the verifier so the clock can be moved."""
        offer = build_offer(
            agent_id="agent-a", resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE,
            pay_to=PAY_TO, policy=OfferPolicy(ttl_seconds=60),
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        ledger.insert_offer(offer, sign(offer, TEST_SECRET))

        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        payload = {
            "offerId": offer["offerId"],
            "agentId": "agent-a",
            "acceptedAt": accepted_at,
            "agentSignature": hmac_sign(agent_commitment_body(offer, accepted_at)),
        }

        with pytest.raises(VerificationError) as exc:
            verify_payment_proof(
                payload=payload,
                offer_row=ledger.get_offer(offer["offerId"]),
                requirements={"amount": str(PRICE_PAISE), "asset": "INR", "payTo": PAY_TO},
                secret=TEST_SECRET,
            )
        assert exc.value.reason == "offer_expired"

    def test_tampered_ledger_row_is_detected(self, ledger):
        """If the stored offer no longer matches its own signature, refuse to settle."""
        offer = build_offer(
            agent_id="agent-a", resource_id=RESOURCE_ID, amount_paise=PRICE_PAISE,
            pay_to=PAY_TO, policy=OfferPolicy(),
        )
        ledger.insert_offer(offer, sign(offer, TEST_SECRET))

        # Someone edits the amount directly in the database.
        with ledger._connect() as conn:  # noqa: SLF001 - deliberately reaching in
            conn.execute(
                "UPDATE offers SET amount_paise = 1 WHERE offer_id = ?", (offer["offerId"],)
            )

        with pytest.raises(VerificationError) as exc:
            verify_payment_proof(
                payload={
                    "offerId": offer["offerId"],
                    "agentId": "agent-a",
                    "acceptedAt": "2026-08-11T00:00:00Z",
                    "agentSignature": "irrelevant",
                },
                offer_row=ledger.get_offer(offer["offerId"]),
                requirements={"amount": "1"},
                secret=TEST_SECRET,
            )
        assert exc.value.reason == "offer_tampered"


# ---------------------------------------------------------------------------
# 5. The real HTTP path, through the Express resource server
# ---------------------------------------------------------------------------


class TestIntegration:
    """Requires both services running. See conftest.live_services."""

    def _sign_for_live(self, quoted: dict, agent_id: str) -> dict:
        accepted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        commitment = dict(quoted["commitmentTemplate"])
        commitment["acceptedAt"] = accepted_at
        secret = "dev-only-shared-secret-change-me"
        return {
            "offerId": quoted["offer"]["offerId"],
            "agentId": agent_id,
            "acceptedAt": accepted_at,
            "agentSignature": hmac_sign(commitment, secret),
        }

    def test_unpaid_request_returns_402_with_an_inr_offer(self, live_services):
        response = httpx.get(live_services["resource"], timeout=15)
        assert response.status_code == 402

        required = json.loads(base64.b64decode(response.headers["payment-required"]))
        assert required["x402Version"] == 2

        offer = required["accepts"][0]
        assert offer["scheme"] == "razorpay-inr"
        assert offer["network"] == "razorpay:inr-test"
        assert offer["asset"] == "INR"
        # Paise, as an integer string — never a float, never rupees.
        assert offer["amount"].isdigit()
        assert offer["extra"]["settlementRail"] == "razorpay"

    def test_paid_request_unlocks_the_resource(self, live_services):
        agent_id = "agent-pytest"
        response = httpx.get(live_services["resource"], timeout=15)
        required = json.loads(base64.b64decode(response.headers["payment-required"]))
        accepted = required["accepts"][0]

        quoted = httpx.post(
            f"{live_services['facilitator']}/offer",
            json={
                "agentId": agent_id,
                "resourceId": RESOURCE_ID,
                "amountPaise": int(accepted["amount"]),
                "payTo": accepted["payTo"],
            },
            timeout=15,
        ).json()

        header = base64.b64encode(
            json.dumps(
                {
                    "x402Version": 2,
                    "accepted": accepted,
                    "payload": self._sign_for_live(quoted, agent_id),
                }
            ).encode()
        ).decode()

        paid = httpx.get(
            live_services["resource"], headers={"X-PAYMENT": header}, timeout=20
        )
        assert paid.status_code == 200
        assert "findings" in paid.json()

        receipt_header = paid.headers.get("payment-response") or paid.headers.get(
            "x-payment-response"
        )
        assert receipt_header, "no settlement receipt returned"
        receipt = json.loads(base64.b64decode(receipt_header))
        assert receipt["success"] is True
        assert receipt["transaction"].startswith("cmt_")

    def test_forged_payment_does_not_unlock_the_resource(self, live_services):
        response = httpx.get(live_services["resource"], timeout=15)
        required = json.loads(base64.b64decode(response.headers["payment-required"]))

        header = base64.b64encode(
            json.dumps(
                {
                    "x402Version": 2,
                    "accepted": required["accepts"][0],
                    "payload": {
                        "offerId": "off_forged",
                        "agentId": "agent-attacker",
                        "acceptedAt": "2026-08-11T00:00:00Z",
                        "agentSignature": "0" * 64,
                    },
                }
            ).encode()
        ).decode()

        forged = httpx.get(
            live_services["resource"], headers={"X-PAYMENT": header}, timeout=20
        )
        assert forged.status_code != 200
        assert "findings" not in forged.text

    def test_free_route_needs_no_payment(self, live_services):
        free_url = live_services["resource"].replace(
            "/premium/market-report", "/free/market-report-preview"
        )
        assert httpx.get(free_url, timeout=15).status_code == 200


# ---------------------------------------------------------------------------
# 6. Multi-visitor isolation (the console's agentId scoping)
# ---------------------------------------------------------------------------


class TestLedgerAgentScoping:
    """Unit-level: the ledger reads that keep one visitor from seeing another's."""

    def _commit(self, ledger, agent_id: str, amount: int, resource_id: str = RESOURCE_ID) -> None:
        offer = build_offer(
            agent_id=agent_id, resource_id=resource_id, amount_paise=amount,
            pay_to=PAY_TO, policy=OfferPolicy(),
        )
        ledger.insert_offer(offer, sign(offer, TEST_SECRET))
        ledger.create_commitment(
            commitment_id=f"cmt_{offer['offerId'][4:]}", offer_id=offer["offerId"],
            agent_id=agent_id, resource_id=resource_id, amount_paise=amount,
            asset="INR", mode="deferred",
        )

    def test_daily_summary_scopes_to_one_agent(self, ledger):
        self._commit(ledger, "agent-a", 500)
        self._commit(ledger, "agent-a", 500)
        self._commit(ledger, "agent-b", 500)

        scoped = ledger.daily_summary(agent_id="agent-a")
        assert scoped["requests"] == 2
        assert scoped["totalPaise"] == 1000
        assert {a["agent_id"] for a in scoped["byAgent"]} == {"agent-a"}

        unscoped = ledger.daily_summary()
        assert unscoped["requests"] == 3

    def test_commitment_amounts_scopes_to_one_agent(self, ledger):
        self._commit(ledger, "agent-a", 50)
        self._commit(ledger, "agent-a", 50)
        self._commit(ledger, "agent-b", 500)

        assert ledger.commitment_amounts(agent_id="agent-a") == [50, 50]
        assert sorted(ledger.commitment_amounts()) == [50, 50, 500]

    def test_list_events_scopes_and_orders_newest_first(self, ledger):
        ledger.log_event("probe", agent_id="agent-a", status="ok")
        ledger.log_event("probe", agent_id="agent-b", status="ok")
        ledger.log_event("probe", agent_id="agent-a", status="ok")

        events = ledger.list_events(agent_id="agent-a")
        assert len(events) == 2
        assert all(e["agentId"] == "agent-a" for e in events)
        assert events[0]["id"] > events[1]["id"]  # newest first

    def test_list_events_since_id_returns_only_newer_rows(self, ledger):
        ledger.log_event("probe", agent_id="agent-a", status="ok")
        checkpoint = ledger.list_events(agent_id="agent-a")[0]["id"]
        ledger.log_event("probe", agent_id="agent-a", status="ok")

        newer = ledger.list_events(agent_id="agent-a", since_id=checkpoint)
        assert len(newer) == 1
        assert newer[0]["id"] > checkpoint

    def test_list_events_limit_is_clamped(self, ledger):
        for _ in range(5):
            ledger.log_event("probe", agent_id="agent-a", status="ok")
        assert len(ledger.list_events(agent_id="agent-a", limit=1000)) <= 200
        assert len(ledger.list_events(agent_id="agent-a", limit=0)) == 1

    def test_list_events_detail_round_trips_as_json(self, ledger):
        ledger.log_event("probe", agent_id="agent-a", status="ok", offerId="off_x", note="hi")
        detail = ledger.list_events(agent_id="agent-a")[0]["detail"]
        assert detail == {"offerId": "off_x", "note": "hi"}


class TestGlobalSettleGuard:
    """A public, multi-visitor deployment must not let one visitor's settle
    button sweep every other visitor's pending commitments."""

    def _reload_with_global_settle(self, monkeypatch, ledger_path, allowed: bool):
        import importlib

        monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
        monkeypatch.setenv("MOCK_RAZORPAY", "true")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
        monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
        monkeypatch.setenv("SETTLEMENT_MODE", "deferred")
        monkeypatch.setenv("ALLOW_GLOBAL_SETTLE", "true" if allowed else "false")

        import main

        importlib.reload(main)
        return main

    def test_bare_settle_batch_rejected_when_disabled(self, ledger_path, monkeypatch):
        from fastapi.testclient import TestClient

        main = self._reload_with_global_settle(monkeypatch, ledger_path, allowed=False)
        with TestClient(main.app) as client:
            response = client.post("/settle-batch", json={})
            assert response.status_code == 400
            assert response.json()["error"] == "agent_id_required"

    def test_scoped_settle_batch_still_works_when_global_disabled(self, ledger_path, monkeypatch):
        from fastapi.testclient import TestClient

        main = self._reload_with_global_settle(monkeypatch, ledger_path, allowed=False)
        with TestClient(main.app) as client:
            envelope = payment_envelope(
                quote(client, agent_id="agent-scoped"), agent_id="agent-scoped"
            )
            client.post("/settle", json=envelope)

            response = client.post("/settle-batch", json={"agentId": "agent-scoped"})
            assert response.status_code == 200
            assert response.json()["batchCount"] == 1

    def test_bare_settle_batch_allowed_by_default(self, ledger_path, monkeypatch):
        """Default stays permissive — nothing about local dev or the scheduler changes."""
        from fastapi.testclient import TestClient

        main = self._reload_with_global_settle(monkeypatch, ledger_path, allowed=True)
        with TestClient(main.app) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-a"), agent_id="agent-a")
            client.post("/settle", json=envelope)

            response = client.post("/settle-batch", json={})
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# 7. The console's server-side agent runner and its read endpoints
# ---------------------------------------------------------------------------


class TestDemoApi:
    """Requires ENABLE_DEMO_API=1 on the running facilitator (CI sets it).

    See conftest.live_services — skips locally when nothing is up,
    REQUIRE_INTEGRATION=1 turns that skip into a failure in CI.
    """

    def test_demo_run_completes_a_real_negotiation(self, live_services):
        response = httpx.post(
            f"{live_services['facilitator']}/demo/run",
            json={
                "sessionId": "pytest-session",
                "agentLabel": "test-bot",
                "resource": "market-report",
            },
            timeout=30,
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["ok"] is True
        assert body["agentId"] == "agent-test-bot-pytestsessio"
        assert body["amountPaise"] == 500
        assert [s["n"] for s in body["steps"]] == [1, 2, 3, 4, 5]
        assert all(s["status"] == "ok" for s in body["steps"])
        assert body["receipt"]["success"] is True
        assert body["receipt"]["transaction"].startswith("cmt_")
        assert "findings" in body["content"]

    def test_demo_run_tamper_flag_produces_a_genuine_rejection(self, live_services):
        response = httpx.post(
            f"{live_services['facilitator']}/demo/run",
            json={
                "sessionId": "pytest-tamper",
                "agentLabel": "forger",
                "resource": "market-report",
                "tamper": True,
            },
            timeout=30,
        )
        assert response.status_code == 200
        body = response.json()

        assert body["ok"] is False
        assert body["error"] == "invalid_signature"
        step_4 = next(s for s in body["steps"] if s["n"] == 4)
        assert step_4["status"] == "failed"
        assert body["content"] is None

    def test_demo_run_never_leaks_the_shared_secret(self, live_services):
        """The one property that makes 'signing happens server-side' a fact
        rather than a slogan: the secret must not be anywhere in the response."""
        secret_candidates = ["dev-only-shared-secret-change-me"]

        response = httpx.post(
            f"{live_services['facilitator']}/demo/run",
            json={"sessionId": "pytest-leak-check", "agentLabel": "auditor"},
            timeout=30,
        )
        raw = response.text
        for secret in secret_candidates:
            assert secret not in raw

    def test_demo_run_rejects_a_malformed_session_id(self, live_services):
        response = httpx.post(
            f"{live_services['facilitator']}/demo/run",
            json={"sessionId": "ab"},  # below min_length=4
            timeout=15,
        )
        assert response.status_code == 422

    def test_economics_reflects_micro_priced_traffic(self, live_services):
        # A unique session per run: the ledger persists across test runs (it is
        # the same file a human might be poking at locally), and an assertion
        # on an *exact* count has to not collide with whatever a previous run
        # already committed under a fixed session id. Same reasoning as the
        # random RUN suffix in tests/ci_batch_flow.py.
        session = f"econcheck{secrets.token_hex(4)}"
        agent_id = _build_agent_id(session, "micro-bot")

        for _ in range(5):
            httpx.post(
                f"{live_services['facilitator']}/demo/run",
                json={"sessionId": session, "agentLabel": "micro-bot", "resource": "api-call"},
                timeout=30,
            )

        response = httpx.get(
            f"{live_services['facilitator']}/economics", params={"agentId": agent_id}, timeout=15
        )
        assert response.status_code == 200
        econ = response.json()["economics"]
        assert econ is not None
        assert econ["commitmentCount"] == 5
        assert econ["belowGatewayMinimum"] == 5
        assert econ["revenueUnreachablePerRequestPaise"] == econ["totalPaise"]

    def test_ledger_summary_and_events_scope_to_one_agent(self, live_services):
        session = f"scopecheck{secrets.token_hex(4)}"
        agent_id = _build_agent_id(session, "scoped-bot")
        httpx.post(
            f"{live_services['facilitator']}/demo/run",
            json={"sessionId": session, "agentLabel": "scoped-bot", "resource": "market-report"},
            timeout=30,
        )

        summary = httpx.get(
            f"{live_services['facilitator']}/ledger/summary",
            params={"agentId": agent_id},
            timeout=15,
        ).json()
        assert summary["requests"] >= 1
        assert all(a["agent_id"] == agent_id for a in summary["byAgent"])

        events = httpx.get(
            f"{live_services['facilitator']}/ledger/events",
            params={"agentId": agent_id, "limit": 20},
            timeout=15,
        ).json()
        assert len(events["events"]) >= 1
        assert all(e["agentId"] == agent_id for e in events["events"])
