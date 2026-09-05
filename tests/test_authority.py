"""Reserved authority: what stands behind a request before content is served.

The claim this file has to make good on is "two concurrent requests cannot
overspend one balance". That is not something a unit test asserting
`available == 500` can establish — it needs threads racing at the same row, and
it needs to run against both engines, because the row-locking behaviour that
makes it true is the database's, not Python's.

Everything else here is the lifecycle around that: capture exactly once,
release on failure, the sweeper, and the invariant
`funded = available + reserved + captured - refunded` holding after every
operation.
"""

from __future__ import annotations

import importlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from authority import CREDIT, PREFUNDED, SIMULATED_RESERVE, AuthorityError, PublisherPolicy
from authority import snapshot_from_row as snap
from conftest import TEST_SECRET

pytestmark = pytest.mark.secure_defaults


def make_account(ledger, *, backing=PREFUNDED, funded=10000, credit_limit=0):
    """An operator, consent, and authority account, wired together."""
    operator_id = f"op_{uuid.uuid4().hex[:12]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    consent_id = f"con_{uuid.uuid4().hex[:12]}"
    account_id = f"aut_{uuid.uuid4().hex[:12]}"

    ledger.create_operator(operator_id=operator_id, display_name="Acme")
    ledger.register_agent(agent_id=agent_id, public_key="x" * 44, algorithm="ed25519")
    ledger.create_consent(
        consent_id=consent_id,
        operator_id=operator_id,
        agent_id=agent_id,
        per_request_limit_paise=0,
        daily_limit_paise=0,
        total_limit_paise=0,
    )
    ledger.create_authority_account(
        account_id=account_id,
        consent_id=consent_id,
        operator_id=operator_id,
        backing=backing,
        funded_paise=funded,
        credit_limit_paise=credit_limit,
    )
    return {
        "operator_id": operator_id,
        "agent_id": agent_id,
        "consent_id": consent_id,
        "account_id": account_id,
    }


def reserve(ledger, ctx, amount, *, offer_id=None, backing=PREFUNDED, credit_limit=0):
    return ledger.reserve_authority(
        reservation_id=f"rsv_{uuid.uuid4().hex[:16]}",
        account_id=ctx["account_id"],
        consent_id=ctx["consent_id"],
        agent_id=ctx["agent_id"],
        offer_id=offer_id or f"off_{uuid.uuid4().hex[:16]}",
        amount_paise=amount,
        ttl_seconds=900,
        backing=backing,
        credit_limit_paise=credit_limit,
    )


def invariant_holds(ledger, consent_id) -> bool:
    """funded = available + reserved + captured - refunded.

    Checked after every operation in these tests rather than only at the end,
    because a transient violation is a real defect: a reader between the two
    halves of a non-atomic update would see money that does not exist.
    """
    a = snap(ledger.get_authority_account(consent_id=consent_id))
    return (
        a.funded_paise
        == a.available_paise + a.reserved_paise + a.captured_paise - a.refunded_paise
    )


class TestReservation:
    def test_a_reservation_moves_available_into_reserved(self, ledger):
        ctx = make_account(ledger, funded=10000)
        reserve(ledger, ctx, 500)

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.available_paise == 9500
        assert account.reserved_paise == 500
        assert account.captured_paise == 0
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_reserving_more_than_available_is_refused(self, ledger):
        ctx = make_account(ledger, funded=400)
        with pytest.raises(AuthorityError) as exc:
            reserve(ledger, ctx, 500)

        assert exc.value.reason == "insufficient_authority"
        # The agent is told what it has, so it can fetch something cheaper.
        assert exc.value.detail["availablePaise"] == 400

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.available_paise == 400, "a refused hold must move nothing"
        assert account.reserved_paise == 0
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_a_refused_reservation_leaves_no_row_behind(self, ledger):
        """A failed hold must not leave a phantom reservation.

        `offer_id` is UNIQUE, so a stranded row from a refused attempt would
        make the *retry* — after a top-up — fail as a duplicate.
        """
        ctx = make_account(ledger, funded=100)
        offer_id = f"off_{uuid.uuid4().hex[:16]}"

        with pytest.raises(AuthorityError):
            reserve(ledger, ctx, 500, offer_id=offer_id)

        assert ledger.get_reservation_by_offer(offer_id) is None

        ledger.fund_authority_account(account_id=ctx["account_id"], amount_paise=1000)
        assert reserve(ledger, ctx, 500, offer_id=offer_id) is not None

    def test_reserving_twice_for_one_offer_holds_one_amount(self, ledger):
        """Idempotent on offer_id.

        /verify may be called speculatively and repeatedly by the middleware.
        Five calls must hold one amount, not five.
        """
        ctx = make_account(ledger, funded=10000)
        offer_id = f"off_{uuid.uuid4().hex[:16]}"

        first = reserve(ledger, ctx, 500, offer_id=offer_id)
        for _ in range(4):
            again = reserve(ledger, ctx, 500, offer_id=offer_id)
            assert again["reservation_id"] == first["reservation_id"]

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.reserved_paise == 500
        assert account.available_paise == 9500


class TestCaptureAndRelease:
    def test_capture_converts_the_hold_into_captured_usage(self, ledger):
        ctx = make_account(ledger, funded=10000)
        held = reserve(ledger, ctx, 500)

        assert ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.reserved_paise == 0
        assert account.captured_paise == 500
        # Captured value is gone from the spendable balance for good.
        assert account.available_paise == 9500
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_capture_happens_exactly_once(self, ledger):
        """A retried settlement must not capture the same authority twice."""
        ctx = make_account(ledger, funded=10000)
        held = reserve(ledger, ctx, 500)

        first = ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )
        second = ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )

        assert first is True
        assert second is False, "second capture must be a no-op"

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.captured_paise == 500
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_release_returns_the_authority(self, ledger):
        """Failed fulfillment must not consume the agent's balance."""
        ctx = make_account(ledger, funded=10000)
        held = reserve(ledger, ctx, 500)

        assert ledger.release_reservation(reservation_id=held["reservation_id"])

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.available_paise == 10000
        assert account.reserved_paise == 0
        assert account.captured_paise == 0
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_a_captured_reservation_cannot_be_released(self, ledger):
        """Otherwise a late failure path would refund authority already spent."""
        ctx = make_account(ledger, funded=10000)
        held = reserve(ledger, ctx, 500)
        ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )

        assert ledger.release_reservation(reservation_id=held["reservation_id"]) is False

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.captured_paise == 500
        assert account.available_paise == 9500

    def test_the_sweeper_returns_expired_holds(self, ledger):
        """A request that dies between reserve and settle must not strand funds."""
        ctx = make_account(ledger, funded=10000)
        reserve(ledger, ctx, 500)

        # Sweep with a cutoff far in the future rather than sleeping.
        swept = ledger.expire_stale_reservations(now="2099-01-01T00:00:00Z")

        assert swept == 1
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.available_paise == 10000
        assert account.reserved_paise == 0
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_the_sweeper_does_not_touch_a_live_hold(self, ledger):
        ctx = make_account(ledger, funded=10000)
        reserve(ledger, ctx, 500)

        assert ledger.expire_stale_reservations(now="2020-01-01T00:00:00Z") == 0
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.reserved_paise == 500


class TestConcurrency:
    """The claim that needs threads, not assertions about a single call."""

    def test_concurrent_reservations_cannot_overspend_one_balance(self, ledger):
        """Ten threads, one ₹10.00 balance, ten ₹5.00 requests.

        Exactly two can succeed. A read-then-write implementation lets more
        through — both callers observe the same balance before either writes,
        and a transaction does not help because it serialises the *writes*,
        not the decision.

        The guarantee comes from `WHERE available_paise >= ?` inside the
        UPDATE, which takes a row lock in both SQLite and Postgres. This test
        runs against whichever engine `ledger_path` selects, so CI proves it on
        real Postgres too.
        """
        ctx = make_account(ledger, funded=1000)

        def attempt(_):
            try:
                reserve(ledger, ctx, 500)
                return True
            except AuthorityError:
                return False
            except Exception:
                # SQLite can raise "database is locked" under contention. That
                # is a failed attempt, not a successful overspend, which is the
                # only thing this test is asserting about.
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        succeeded = sum(results)

        assert succeeded <= 2, f"{succeeded} reservations succeeded against a ₹10.00 balance"
        assert account.available_paise >= 0, "balance went negative — overspend"
        assert account.reserved_paise == succeeded * 500
        assert invariant_holds(ledger, ctx["consent_id"])

    def test_concurrent_captures_of_one_reservation_capture_once(self, ledger):
        """Duplicate settlements arriving together must capture one amount."""
        ctx = make_account(ledger, funded=10000)
        held = reserve(ledger, ctx, 500)

        def attempt(_):
            try:
                return ledger.capture_reservation(
                    reservation_id=held["reservation_id"], commitment_id="cmt_1"
                )
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))

        assert sum(results) == 1, "capture must succeed exactly once"
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.captured_paise == 500
        assert invariant_holds(ledger, ctx["consent_id"])


class TestCreditBacking:
    def test_credit_draws_against_a_limit_not_a_balance(self, ledger):
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=1000)
        reserve(ledger, ctx, 500, backing=CREDIT, credit_limit=1000)

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.reserved_paise == 500
        # No funds moved: there are none. Available stays zero.
        assert account.available_paise == 0
        assert account.spendable_paise == 500

    def test_credit_beyond_the_limit_is_refused(self, ledger):
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=1000)
        reserve(ledger, ctx, 800, backing=CREDIT, credit_limit=1000)

        with pytest.raises(AuthorityError) as exc:
            reserve(ledger, ctx, 500, backing=CREDIT, credit_limit=1000)

        assert exc.value.reason == "credit_limit_exceeded"

    def test_credit_backed_usage_is_never_described_as_funded(self, ledger):
        """The property a publisher refusing credit actually checks."""
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=1000)
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))

        assert account.is_funded is False
        assert account.as_dict()["isFunded"] is False

    def test_exposure_tracks_captured_minus_refunded(self, ledger):
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=5000)
        held = reserve(ledger, ctx, 500, backing=CREDIT, credit_limit=5000)
        ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )

        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        assert account.exposure_paise == 500


class TestSimulatedReserveIsLabelled:
    def test_a_simulated_reserve_account_says_so_in_its_payload(self, ledger):
        """A client must not have to read a README to know this is staged."""
        ctx = make_account(ledger, backing=SIMULATED_RESERVE, funded=10000)
        payload = snap(ledger.get_authority_account(consent_id=ctx["consent_id"])).as_dict()

        assert payload["simulated"] is True
        assert payload["backing"] == "simulated_reserve"

    def test_a_prefunded_account_is_not_labelled_simulated(self, ledger):
        ctx = make_account(ledger, backing=PREFUNDED, funded=10000)
        payload = snap(ledger.get_authority_account(consent_id=ctx["consent_id"])).as_dict()
        assert payload["simulated"] is False


class TestPublisherPolicy:
    """What this publisher will serve content against."""

    def test_a_default_policy_accepts_anything(self, ledger):
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=1000)
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        PublisherPolicy().check(account, amount_paise=500)

    def test_requiring_funded_authority_refuses_credit(self, ledger):
        ctx = make_account(ledger, backing=CREDIT, funded=0, credit_limit=1000)
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))

        with pytest.raises(AuthorityError) as exc:
            PublisherPolicy(require_funded_authority=True).check(account, amount_paise=500)
        assert exc.value.reason == "funded_authority_required"

    def test_requiring_funded_authority_accepts_prefunded(self, ledger):
        ctx = make_account(ledger, backing=PREFUNDED, funded=10000)
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))
        PublisherPolicy(require_funded_authority=True).check(account, amount_paise=500)

    def test_an_operator_allowlist_refuses_others(self, ledger):
        ctx = make_account(ledger, funded=10000)
        account = snap(ledger.get_authority_account(consent_id=ctx["consent_id"]))

        with pytest.raises(AuthorityError) as exc:
            PublisherPolicy(allowed_operator_ids=frozenset({"op_someone_else"})).check(
                account, amount_paise=500
            )
        assert exc.value.reason == "operator_not_allowed"

    def test_the_unsecured_exposure_ceiling_only_binds_unfunded_backings(self, ledger):
        """Prefunded value is already there; counting it would refuse safe traffic."""
        funded = make_account(ledger, backing=PREFUNDED, funded=100000)
        credit = make_account(ledger, backing=CREDIT, funded=0, credit_limit=100000)

        policy = PublisherPolicy(max_unsecured_exposure_paise=1000)

        held = reserve(ledger, credit, 900, backing=CREDIT, credit_limit=100000)
        ledger.capture_reservation(
            reservation_id=held["reservation_id"], commitment_id="cmt_1"
        )

        # The prefunded account is unaffected by the ceiling.
        policy.check(
            snap(ledger.get_authority_account(consent_id=funded["consent_id"])),
            amount_paise=50000,
        )

        with pytest.raises(AuthorityError) as exc:
            policy.check(
                snap(ledger.get_authority_account(consent_id=credit["consent_id"])),
                amount_paise=500,
            )
        assert exc.value.reason == "unsecured_exposure_exceeded"

    def test_policy_reads_from_the_environment(self):
        policy = PublisherPolicy.from_env(
            {
                "PUBLISHER_REQUIRE_FUNDED_AUTHORITY": "true",
                "PUBLISHER_MAX_UNSECURED_EXPOSURE_PAISE": "5000",
                "PUBLISHER_ALLOWED_OPERATORS": "op_a op_b",
            }
        )
        assert policy.require_funded_authority is True
        assert policy.max_unsecured_exposure_paise == 5000
        assert policy.allowed_operator_ids == frozenset({"op_a", "op_b"})


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------


def _reload_main(monkeypatch, ledger_path, **env):
    monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("MOCK_RAZORPAY", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
    monkeypatch.setenv("SETTLEMENT_MODE", "deferred")
    monkeypatch.setenv("CONTROL_PLANE_BOOTSTRAP_TOKEN", "bootstrap-token-for-tests")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import main

    importlib.reload(main)
    return main


class TestAuthorityIsRequiredEndToEnd:
    def test_a_quote_without_authority_cannot_be_verified(self, ledger_path, monkeypatch):
        """The production default: no reservation, no content.

        /verify is the last point before the publisher's handler runs, so a
        refusal here is a refusal to release content — which is the whole
        claim.
        """
        from fastapi.testclient import TestClient

        # HMAC fallback on, so the *signature* verifies and the refusal that
        # comes back is the authority one. Without it verification fails first
        # with `agent_not_registered` and this test would pass for the wrong
        # reason — proving key enforcement works, not authority enforcement.
        main = _reload_main(
            monkeypatch, ledger_path, REQUIRE_CONSENT="false", ALLOW_HMAC_FALLBACK="true"
        )
        with TestClient(main.app) as client:
            quoted = client.post(
                "/offer",
                json={
                    "agentId": "agent-unbacked",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )
            assert quoted.status_code == 200, quoted.text

            from test_full_flow import payment_envelope

            verified = client.post(
                "/verify", json=payment_envelope(quoted.json(), agent_id="agent-unbacked")
            )

        body = verified.json()
        assert body["isValid"] is False
        assert body["invalidReason"] == "no_authority"
        assert "unbacked promise" in body["invalidMessage"]
