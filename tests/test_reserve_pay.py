"""UPI Reserve Pay as a second settlement instrument.

The README claims the commitment ledger is indifferent to what settles it and
that swapping Payment Links for Reserve Pay touches one file. These tests are
what makes that claim checkable rather than rhetorical: the same batch
settlement runs through a mandate debit instead of a hosted link, and the
money handling either side of it is untouched.

The one that matters most is `test_switching_instrument_changes_nothing_about
_the_commitment_lifecycle` — if that ever fails, the seam the claim rests on
has stopped being a seam.
"""

from __future__ import annotations

import importlib

import pytest
from conftest import TEST_SECRET
from ledger import Ledger
from razorpay_client import RazorpayConfigError, RazorpayGateway
from reserve_pay import MockReservePay, ReservePayError
from test_full_flow import PRICE_PAISE, payment_envelope, quote


def _reload_main(monkeypatch, ledger_path, **env):
    monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("MOCK_RAZORPAY", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
    monkeypatch.setenv("SETTLEMENT_MODE", "deferred")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import main

    importlib.reload(main)
    return main


def _client(main):
    from fastapi.testclient import TestClient

    return TestClient(main.app)


class TestMandate:
    def test_a_mandate_id_is_stable_for_an_agent(self):
        """Derived, not generated. On a serverless deployment a random id
        would be lost on the next cold start and the same agent would look
        like a new counterparty every few minutes."""
        rp = MockReservePay()
        assert rp.ensure_mandate("agent-x").mandate_id == rp.ensure_mandate("agent-x").mandate_id

    def test_different_agents_get_different_mandates(self):
        rp = MockReservePay()
        assert rp.ensure_mandate("agent-a").mandate_id != rp.ensure_mandate("agent-b").mandate_id

    def test_a_debit_has_no_url_to_open(self):
        """The whole point of the instrument. A Payment Link is a page a human
        opens; inventing a URL here would hide the one difference that matters
        for machine-to-machine payments."""
        rp = MockReservePay()
        debit = rp.debit(
            mandate=rp.ensure_mandate("agent-x"), amount_paise=500, reference_id="batch_1"
        )
        assert debit["short_url"] is None
        assert debit["instrument"] == "reserve_pay"
        # Captured, not "created" — the money has already moved, so no webhook
        # is needed to learn that it settled.
        assert debit["status"] == "captured"

    def test_a_debit_draws_the_block_down(self):
        rp = MockReservePay(block_paise=1000)
        debit = rp.debit(
            mandate=rp.ensure_mandate("agent-x"),
            amount_paise=400,
            reference_id="batch_1",
            already_debited_paise=500,
        )
        assert debit["mandateRemainingPaise"] == 100

    def test_a_debit_over_the_block_is_refused(self):
        rp = MockReservePay(block_paise=1000)
        with pytest.raises(ReservePayError) as excinfo:
            rp.debit(
                mandate=rp.ensure_mandate("agent-x"),
                amount_paise=600,
                reference_id="batch_1",
                already_debited_paise=500,
            )
        assert "re-authorising" in str(excinfo.value)

    def test_a_zero_debit_is_refused(self):
        rp = MockReservePay()
        with pytest.raises(ReservePayError):
            rp.debit(mandate=rp.ensure_mandate("agent-x"), amount_paise=0, reference_id="b")


class TestGatewayDispatch:
    def test_create_charge_defaults_to_a_payment_link(self, monkeypatch):
        monkeypatch.delenv("SETTLEMENT_INSTRUMENT", raising=False)
        gateway = RazorpayGateway(mock=True)
        charge = gateway.create_charge(
            amount_paise=500, description="d", reference_id="batch_1", agent_id="agent-x"
        )
        assert charge["instrument"] == "payment_link"
        assert charge["short_url"]  # a link has a page

    def test_create_charge_can_be_switched_to_reserve_pay(self, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_INSTRUMENT", "reserve_pay")
        gateway = RazorpayGateway(mock=True)
        charge = gateway.create_charge(
            amount_paise=500, description="d", reference_id="batch_1", agent_id="agent-x"
        )
        assert charge["instrument"] == "reserve_pay"
        assert charge["short_url"] is None
        assert charge["id"].startswith("rpdebit_")

    def test_an_unknown_instrument_is_refused_at_startup(self, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_INSTRUMENT", "carrier_pigeon")
        with pytest.raises(RazorpayConfigError):
            RazorpayGateway(mock=True)

    def test_reserve_pay_is_refused_against_real_credentials(self, monkeypatch):
        """It is a simulation with fabricated ids. A service holding real
        test-mode keys that reports fake debits as settlements is worse than
        one that refuses to start."""
        monkeypatch.setenv("SETTLEMENT_INSTRUMENT", "reserve_pay")
        with pytest.raises(RazorpayConfigError) as excinfo:
            RazorpayGateway(key_id="rzp_test_abc", key_secret="secret", mock=False)
        assert "simulation" in str(excinfo.value)


class TestSettlementThroughAMandate:
    def test_a_batch_settles_against_a_mandate(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-rp"), agent_id="agent-rp")
            client.post("/settle", json=envelope)

            body = client.post("/settle-batch", json={"agentId": "agent-rp"}).json()

        batch = body["batches"][0]
        assert batch["totalPaise"] == PRICE_PAISE
        assert batch["paymentLinkId"].startswith("rpdebit_")
        assert batch["paymentLinkUrl"] is None

    def test_a_mandate_debit_is_collected_without_a_webhook(self, ledger_path, monkeypatch):
        """A Payment Link is an invoice until its webhook lands. A debit has
        already taken the money, and there is no webhook coming — so
        `collectedPaise` has to be right at settlement time or it is wrong
        forever."""
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-rp2"), agent_id="agent-rp2")
            client.post("/settle", json=envelope)
            client.post("/settle-batch", json={"agentId": "agent-rp2"})

        summary = Ledger(str(ledger_path)).daily_summary(agent_id="agent-rp2")
        assert summary["committedPaise"] == PRICE_PAISE
        assert summary["collectedPaise"] == PRICE_PAISE
        assert summary["paidBatches"] == 1

    def test_a_payment_link_is_still_only_billed_not_collected(self, ledger_path, monkeypatch):
        """The contrast that makes the previous test meaningful."""
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="payment_link")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-pl"), agent_id="agent-pl")
            client.post("/settle", json=envelope)
            client.post("/settle-batch", json={"agentId": "agent-pl"})

        summary = Ledger(str(ledger_path)).daily_summary(agent_id="agent-pl")
        assert summary["committedPaise"] == PRICE_PAISE
        assert summary["collectedPaise"] == 0

    def test_the_instrument_is_recorded_on_the_batch(self, ledger_path, monkeypatch):
        """Without this column a debit and a *failed* link look identical —
        both have a null URL."""
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-rp3"), agent_id="agent-rp3")
            client.post("/settle", json=envelope)
            body = client.post("/settle-batch", json={"agentId": "agent-rp3"}).json()

        batch = Ledger(str(ledger_path)).get_batch(body["batches"][0]["batchId"])
        assert batch["instrument"] == "reserve_pay"
        assert batch["payment_link_url"] is None

    def test_debits_accumulate_against_the_block(self, ledger_path, monkeypatch):
        """The block is drawn down across the day, and the remaining amount
        comes from the ledger — not a counter in the gateway, which would
        reset on a serverless cold start and let the same block be spent
        twice."""
        main = _reload_main(
            monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay",
            AGENT_DAILY_CAP_PAISE="0",
        )
        with _client(main) as client:
            for _ in range(2):
                envelope = payment_envelope(
                    quote(client, agent_id="agent-rp4"), agent_id="agent-rp4"
                )
                client.post("/settle", json=envelope)
                client.post("/settle-batch", json={"agentId": "agent-rp4"})

        book = Ledger(str(ledger_path))
        assert book.debited_today(agent_id="agent-rp4") == PRICE_PAISE * 2
        # Payment Links draw on no block, so they must not count.
        assert book.debited_today(agent_id="agent-nobody") == 0

    def test_switching_instrument_changes_nothing_about_the_commitment_lifecycle(
        self, ledger_path, monkeypatch, tmp_path
    ):
        """The claim under test: the ledger is indifferent to what settles it.

        Same traffic through both instruments; the commitment side of the
        ledger must come out identical. If this fails, the seam the README's
        "only one file changes" claim rests on has stopped being a seam.
        """
        outcomes = {}
        for instrument in ("payment_link", "reserve_pay"):
            path = tmp_path / f"{instrument}.db"
            main = _reload_main(
                monkeypatch, path, SETTLEMENT_INSTRUMENT=instrument
            )
            with _client(main) as client:
                envelope = payment_envelope(
                    quote(client, agent_id="agent-cmp"), agent_id="agent-cmp"
                )
                client.post("/settle", json=envelope)
                body = client.post("/settle-batch", json={"agentId": "agent-cmp"}).json()

            book = Ledger(str(path))
            summary = book.daily_summary(agent_id="agent-cmp")
            outcomes[instrument] = {
                "batchCount": body["batchCount"],
                "committed": summary["committedPaise"],
                "requests": summary["requests"],
                "pending": len(book.pending_commitments(agent_id="agent-cmp")),
                "commitmentTotal": book.committed_today(agent_id="agent-cmp"),
            }

        assert outcomes["payment_link"] == outcomes["reserve_pay"], outcomes


class TestInstrumentIsDiscoverable:
    def test_supported_advertises_the_instrument(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay")
        with _client(main) as client:
            extra = client.get("/supported").json()["kinds"][0]["extra"]
        assert extra["settlementInstrument"] == "reserve_pay"

    def test_health_reports_the_instrument(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, SETTLEMENT_INSTRUMENT="reserve_pay")
        with _client(main) as client:
            assert client.get("/health").json()["settlementInstrument"] == "reserve_pay"
