"""Spending controls — what the facilitator refuses to let an agent do.

Before these, the only limit was `MAX_OFFER_PAISE`: a ceiling on one quote.
An agent could be quoted ₹1,000 ten thousand times and nothing objected.
"No transaction may exceed X" is a transaction-size limit, not a spending
limit, and the gap between those two is what this covers.

The load-bearing tests here are the ones about *where* the cap is enforced:

  * `test_the_cap_is_enforced_in_sql_not_by_the_pre_read` — the check has to
    live inside the INSERT, or two concurrent settlements both pass it.
  * `test_a_capped_settlement_leaves_the_offer_spendable` — a refusal must not
    burn the agent's offer.
  * `test_daily_cap_exceeded_is_not_a_value_error` — `main.py` maps ValueError
    from create_commitment to "already spent, return the existing
    commitment". A cap breach arriving as ValueError would answer a refused
    payment with somebody else's receipt.
"""

from __future__ import annotations

import importlib

import pytest
from conftest import TEST_SECRET
from ledger import DailyCapExceeded, Ledger, utc_now
from limits import LimitExceeded, SpendPolicy
from test_full_flow import PAY_TO, PRICE_PAISE, RESOURCE_ID, payment_envelope, quote


def _reload_main(monkeypatch, ledger_path, **env):
    """A facilitator reloaded with a given spend policy."""
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


# ---------------------------------------------------------------------------
# The policy object, before any HTTP
# ---------------------------------------------------------------------------


class TestSpendPolicy:
    def test_cap_refuses_when_the_total_would_breach_it(self):
        policy = SpendPolicy(daily_cap_paise=1000)
        with pytest.raises(LimitExceeded) as excinfo:
            policy.check_daily_cap("agent-x", committed_paise=800, amount_paise=500)
        assert excinfo.value.reason == "daily_cap_exceeded"
        assert excinfo.value.detail["remainingPaise"] == 200

    def test_spending_exactly_to_the_cap_is_allowed(self):
        """Off-by-one matters when it is money: the cap is a ceiling you may
        reach, not one you must stay under."""
        SpendPolicy(daily_cap_paise=1000).check_daily_cap(
            "agent-x", committed_paise=500, amount_paise=500
        )

    def test_zero_cap_means_unlimited(self):
        SpendPolicy(daily_cap_paise=0).check_daily_cap(
            "agent-x", committed_paise=10**9, amount_paise=10**9
        )

    def test_rate_limit_refuses_at_the_limit(self):
        policy = SpendPolicy(offer_rate_per_minute=10)
        with pytest.raises(LimitExceeded) as excinfo:
            policy.check_offer_rate("agent-x", recent_offers=10)
        assert excinfo.value.reason == "offer_rate_exceeded"

    def test_rate_limit_allows_below_it(self):
        SpendPolicy(offer_rate_per_minute=10).check_offer_rate("agent-x", recent_offers=9)

    def test_zero_rate_means_unlimited(self):
        SpendPolicy(offer_rate_per_minute=0).check_offer_rate("agent-x", recent_offers=10**6)

    def test_frozen_agent_is_refused(self):
        policy = SpendPolicy(frozen_agents=frozenset({"agent-bad"}))
        with pytest.raises(LimitExceeded) as excinfo:
            policy.check_admission("agent-bad")
        assert excinfo.value.reason == "agent_frozen"
        policy.check_admission("agent-fine")

    def test_global_switch_refuses_everyone(self):
        policy = SpendPolicy(accept_payments=False)
        with pytest.raises(LimitExceeded) as excinfo:
            policy.check_admission("agent-anyone")
        assert excinfo.value.reason == "payments_suspended"

    def test_from_env_parses_a_frozen_list(self, monkeypatch):
        monkeypatch.setenv("FROZEN_AGENTS", " agent-a , agent-b ,, ")
        assert SpendPolicy.from_env().frozen_agents == frozenset({"agent-a", "agent-b"})


# ---------------------------------------------------------------------------
# The binding check, in the ledger
# ---------------------------------------------------------------------------


class TestLedgerCapEnforcement:
    def _open_offer(
        self,
        ledger: Ledger,
        agent_id: str,
        offer_id: str,
        paise: int,
        issued_at: str | None = None,
    ) -> None:
        """Inserts an open offer. `issued_at` defaults to now, because the
        rate limit reads that column and a hardcoded past timestamp would sit
        outside its window."""
        ledger.insert_offer(
            {
                "offerId": offer_id,
                "agentId": agent_id,
                "resourceId": RESOURCE_ID,
                "resourceUrl": None,
                "amountPaise": paise,
                "asset": "INR",
                "scheme": "razorpay-inr",
                "network": "razorpay:inr-test",
                "payTo": PAY_TO,
                "nonce": offer_id,
                "issuedAt": issued_at or utc_now(),
                "expiresAt": "2999-01-01T00:00:00Z",
            },
            "sig",
        )

    def _commit(self, ledger, agent_id, offer_id, paise, cap):
        return ledger.create_commitment(
            commitment_id=f"cmt_{offer_id}",
            offer_id=offer_id,
            agent_id=agent_id,
            resource_id=RESOURCE_ID,
            amount_paise=paise,
            asset="INR",
            mode="deferred",
            daily_cap_paise=cap,
        )

    def test_a_commitment_within_the_cap_is_booked(self, ledger):
        self._open_offer(ledger, "agent-ok", "off_1", 500)
        result = self._commit(ledger, "agent-ok", "off_1", 500, cap=1000)
        assert result["amountPaise"] == 500

    def test_a_commitment_over_the_cap_is_refused(self, ledger):
        self._open_offer(ledger, "agent-cap", "off_1", 800)
        self._commit(ledger, "agent-cap", "off_1", 800, cap=1000)

        self._open_offer(ledger, "agent-cap", "off_2", 500)
        with pytest.raises(DailyCapExceeded) as excinfo:
            self._commit(ledger, "agent-cap", "off_2", 500, cap=1000)

        assert excinfo.value.committed_paise == 800
        assert excinfo.value.remaining_paise == 200

    def test_a_capped_settlement_leaves_the_offer_spendable(self, ledger):
        """A refusal rolls back, so the agent has not lost the offer. Burning
        it would make hitting a limit cost real money."""
        self._open_offer(ledger, "agent-roll", "off_1", 1000)
        self._commit(ledger, "agent-roll", "off_1", 1000, cap=1000)

        self._open_offer(ledger, "agent-roll", "off_2", 500)
        with pytest.raises(DailyCapExceeded):
            self._commit(ledger, "agent-roll", "off_2", 500, cap=1000)

        assert ledger.get_offer("off_2")["status"] == "open"
        assert ledger.get_commitment_by_offer("off_2") is None

    def test_the_cap_is_enforced_in_sql_not_by_the_pre_read(self, ledger, monkeypatch):
        """The check has to live inside the INSERT.

        A separate "read the total, then decide" would let two concurrent
        settlements both pass and land the agent one payment over. Proven here
        by making the pre-read lie: even told the agent has spent nothing, the
        statement must still refuse.
        """
        self._open_offer(ledger, "agent-race", "off_1", 900)
        self._commit(ledger, "agent-race", "off_1", 900, cap=1000)

        monkeypatch.setattr(
            Ledger, "committed_today", lambda self, **kwargs: 0, raising=True
        )
        assert ledger.committed_today(agent_id="agent-race") == 0  # the lie is in place

        self._open_offer(ledger, "agent-race", "off_2", 500)
        with pytest.raises(DailyCapExceeded):
            self._commit(ledger, "agent-race", "off_2", 500, cap=1000)

    def test_daily_cap_exceeded_is_not_a_value_error(self):
        """main.py treats ValueError from create_commitment as 'already spent,
        return the existing commitment'. A cap breach arriving that way would
        answer a refused payment with somebody else's receipt."""
        assert not issubclass(DailyCapExceeded, ValueError)

    def test_settled_commitments_still_count_against_the_cap(self, ledger):
        """Otherwise an agent resets its own limit by triggering a batch."""
        self._open_offer(ledger, "agent-settled", "off_1", 900)
        self._commit(ledger, "agent-settled", "off_1", 900, cap=1000)

        ledger.record_batch(
            batch_id="batch_1",
            agent_id="agent-settled",
            settle_date=ledger.pending_commitments(agent_id="agent-settled")[0]["settle_date"],
            commitment_ids=["cmt_off_1"],
            total_paise=900,
            payment_link_id="plink_1",
            payment_link_url=None,
            status="created",
            razorpay_mode="mock",
        )
        assert ledger.pending_commitments(agent_id="agent-settled") == []
        assert ledger.committed_today(agent_id="agent-settled") == 900

    def test_the_cap_is_per_agent(self, ledger):
        self._open_offer(ledger, "agent-a", "off_a", 1000)
        self._commit(ledger, "agent-a", "off_a", 1000, cap=1000)

        self._open_offer(ledger, "agent-b", "off_b", 1000)
        assert self._commit(ledger, "agent-b", "off_b", 1000, cap=1000)["amountPaise"] == 1000

    def test_recent_offer_count_only_counts_one_agent(self, ledger):
        self._open_offer(ledger, "agent-noisy", "off_1", 500)
        self._open_offer(ledger, "agent-noisy", "off_2", 500)
        self._open_offer(ledger, "agent-quiet", "off_3", 500)

        assert ledger.recent_offer_count(agent_id="agent-noisy") == 2
        assert ledger.recent_offer_count(agent_id="agent-quiet") == 1

    def test_older_quotes_fall_outside_the_rate_window(self, ledger):
        """The rate limit is a window, not a lifetime total — otherwise an
        agent is permanently throttled by traffic from this morning."""
        self._open_offer(ledger, "agent-hist", "off_old", 500, issued_at="2026-08-01T00:00:00Z")
        self._open_offer(ledger, "agent-hist", "off_new", 500)

        assert ledger.recent_offer_count(agent_id="agent-hist") == 1
        assert ledger.recent_offer_count(agent_id="agent-hist", within_seconds=10**9) == 2


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


class TestOfferEndpointLimits:
    def test_over_cap_quote_is_refused_before_anything_is_written(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="600")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-h"), agent_id="agent-h")
            assert client.post("/settle", json=envelope).json()["success"] is True

            # ₹5 committed, cap ₹6 — a second ₹5 quote cannot be honoured.
            response = client.post(
                "/offer",
                json={
                    "agentId": "agent-h",
                    "resourceId": RESOURCE_ID,
                    "amountPaise": PRICE_PAISE,
                    "payTo": PAY_TO,
                },
            )
            assert response.status_code == 403
            body = response.json()
            assert body["error"] == "daily_cap_exceeded"
            assert body["remainingPaise"] == 100

    def test_frozen_agent_cannot_get_a_quote(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, FROZEN_AGENTS="agent-frozen")
        with _client(main) as client:
            response = client.post(
                "/offer",
                json={
                    "agentId": "agent-frozen",
                    "resourceId": RESOURCE_ID,
                    "amountPaise": PRICE_PAISE,
                    "payTo": PAY_TO,
                },
            )
            assert response.status_code == 403
            assert response.json()["error"] == "agent_frozen"

    def test_suspending_payments_refuses_everyone(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, ACCEPT_PAYMENTS="false")
        with _client(main) as client:
            response = client.post(
                "/offer",
                json={
                    "agentId": "agent-anyone",
                    "resourceId": RESOURCE_ID,
                    "amountPaise": PRICE_PAISE,
                    "payTo": PAY_TO,
                },
            )
            assert response.status_code == 403
            assert response.json()["error"] == "payments_suspended"

    def test_quote_flooding_is_rate_limited_with_a_429(self, ledger_path, monkeypatch):
        main = _reload_main(
            monkeypatch, ledger_path, AGENT_OFFER_RATE_PER_MINUTE="3", AGENT_DAILY_CAP_PAISE="0"
        )
        with _client(main) as client:
            body = {
                "agentId": "agent-flood",
                "resourceId": RESOURCE_ID,
                "amountPaise": PRICE_PAISE,
                "payTo": PAY_TO,
            }
            codes = [client.post("/offer", json=body).status_code for _ in range(5)]

        assert codes[:3] == [200, 200, 200]
        # 429, not 403: this one is worth retrying.
        assert codes[3:] == [429, 429]

    def test_a_refused_quote_is_written_to_the_audit_log(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, FROZEN_AGENTS="agent-logged")
        with _client(main) as client:
            client.post(
                "/offer",
                json={
                    "agentId": "agent-logged",
                    "resourceId": RESOURCE_ID,
                    "amountPaise": PRICE_PAISE,
                    "payTo": PAY_TO,
                },
            )

        events = Ledger(str(ledger_path)).list_events(agent_id="agent-logged")
        assert [e["event"] for e in events] == ["offer_refused_by_policy"]
        assert events[0]["detail"]["reason"] == "agent_frozen"


class TestSettleEndpointLimits:
    def test_settlement_over_the_cap_fails_in_the_x402_shape(self, ledger_path, monkeypatch):
        """A quote issued before the cap was reached must still be refused at
        settle — that is the binding check."""
        main = _reload_main(monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="600")
        with _client(main) as client:
            # Two quotes taken up front, while both are still permissible.
            first = quote(client, agent_id="agent-s")
            second = quote(client, agent_id="agent-s")

            assert client.post(
                "/settle", json=payment_envelope(first, agent_id="agent-s")
            ).json()["success"] is True

            body = client.post(
                "/settle", json=payment_envelope(second, agent_id="agent-s")
            ).json()
            assert body["success"] is False
            assert body["errorReason"] == "daily_cap_exceeded"

    def test_a_refused_settlement_books_nothing(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="600")
        with _client(main) as client:
            first = quote(client, agent_id="agent-s2")
            second = quote(client, agent_id="agent-s2")
            client.post("/settle", json=payment_envelope(first, agent_id="agent-s2"))
            client.post("/settle", json=payment_envelope(second, agent_id="agent-s2"))

        book = Ledger(str(ledger_path))
        assert book.committed_today(agent_id="agent-s2") == PRICE_PAISE
        assert book.get_commitment_by_offer(second["offer"]["offerId"]) is None


class TestLimitsAreDiscoverable:
    def test_supported_advertises_the_policy(self, ledger_path, monkeypatch):
        main = _reload_main(
            monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="7500",
            AGENT_OFFER_RATE_PER_MINUTE="42",
        )
        with _client(main) as client:
            extra = client.get("/supported").json()["kinds"][0]["extra"]

        assert extra["limits"]["dailyCapPaise"] == 7500
        assert extra["limits"]["offerRatePerMinute"] == 42
        assert extra["limits"]["acceptingPayments"] is True
        assert extra["maxOfferPaise"] > 0

    def test_an_agent_can_read_its_own_remaining_budget(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="2000")
        with _client(main) as client:
            envelope = payment_envelope(quote(client, agent_id="agent-r"), agent_id="agent-r")
            client.post("/settle", json=envelope)

            body = client.get("/agents/agent-r/limits").json()

        assert body["committedPaise"] == PRICE_PAISE
        assert body["dailyCapPaise"] == 2000
        assert body["remainingPaise"] == 2000 - PRICE_PAISE
        assert body["frozen"] is False

    def test_unlimited_is_reported_as_null_not_zero(self, ledger_path, monkeypatch):
        """`0` in config means unlimited; reporting it as `0` to a client would
        read as 'you may spend nothing'."""
        main = _reload_main(monkeypatch, ledger_path, AGENT_DAILY_CAP_PAISE="0")
        with _client(main) as client:
            body = client.get("/agents/agent-u/limits").json()

        assert body["dailyCapPaise"] is None
        assert body["remainingPaise"] is None
