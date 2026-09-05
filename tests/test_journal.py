"""Accounting invariants: the rules that must hold for every input, not some.

A worked example proves a posting is right once. These are the properties that
have to be true always — debits equal credits, a replayed command posts
nothing, a refund allocation sums exactly — so the ones that can be stated over
all inputs are tested with hypothesis rather than with the handful of cases
someone happened to think of.

The most valuable test in this file is `test_allocation_always_sums_exactly`.
Splitting a refund across an aggregate collection is where integer money
usually goes wrong: the obvious implementation rounds each share and the total
comes out a paisa short, leaving a balance nobody can explain and nobody can
clear. Hypothesis will find that in seconds; a hand-written example almost
never does.
"""

from __future__ import annotations

import uuid

import journal
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from journal import (
    CREDIT,
    DEBIT,
    Entry,
    UnbalancedTransaction,
    allocate_refund,
    build_transaction,
)

pytestmark = pytest.mark.secure_defaults


# ---------------------------------------------------------------------------
# Building a transaction
# ---------------------------------------------------------------------------


class TestBalancedByConstruction:
    def test_a_balanced_transaction_builds(self):
        txn = build_transaction(
            command_ref="cmd_1",
            txn_type=journal.CAPTURE_USAGE,
            description="test",
            entries=[
                Entry(journal.AGENT_RECEIVABLE, DEBIT, 500),
                Entry(journal.PUBLISHER_PAYABLE, CREDIT, 500),
            ],
        )
        assert txn.total_debits == txn.total_credits == 500

    def test_an_unbalanced_transaction_is_refused_at_build_time(self):
        """Refused, not stored and reported later.

        A journal that can hold an unbalanced transaction is not a journal —
        every figure derived from it becomes suspect.
        """
        with pytest.raises(UnbalancedTransaction) as exc:
            build_transaction(
                command_ref="cmd_2",
                txn_type=journal.CAPTURE_USAGE,
                description="test",
                entries=[
                    Entry(journal.AGENT_RECEIVABLE, DEBIT, 500),
                    Entry(journal.PUBLISHER_PAYABLE, CREDIT, 400),
                ],
            )
        # The message names the gap, so the fix does not need a debugger.
        assert "100" in str(exc.value)

    def test_a_single_legged_transaction_is_refused(self):
        with pytest.raises(UnbalancedTransaction):
            build_transaction(
                command_ref="cmd_3",
                txn_type=journal.CAPTURE_USAGE,
                description="test",
                entries=[Entry(journal.AGENT_RECEIVABLE, DEBIT, 500)],
            )

    @pytest.mark.parametrize("amount", [0, -1, -500])
    def test_a_non_positive_amount_is_refused(self, amount):
        """The direction carries the sign; a negative amount is ambiguous."""
        with pytest.raises(ValueError):
            Entry(journal.AGENT_RECEIVABLE, DEBIT, amount)

    def test_a_boolean_amount_is_refused(self):
        """`bool` is an `int` subclass, so `True` would otherwise post 1 paisa."""
        with pytest.raises(TypeError):
            Entry(journal.AGENT_RECEIVABLE, DEBIT, True)

    def test_an_unknown_account_is_refused(self):
        with pytest.raises(ValueError) as exc:
            Entry("9999", DEBIT, 500)
        assert "unknown account" in str(exc.value)


class TestTheEconomicEvents:
    """Each builder is one event. These check the direction of each leg."""

    def test_capture_does_not_credit_revenue(self):
        """The single most important assertion in this file.

        Content was delivered and a receivable exists. No money has arrived.
        Crediting a revenue account here would be this project's own argument
        made wrong, expressed in accounts instead of a dashboard tile.
        """
        txn = journal.capture_usage(
            command_ref="cap_1",
            amount_paise=500,
            agent_id="agent-a",
            operator_id="op_1",
            merchant_id="mer_1",
            commitment_id="cmt_1",
        )
        accounts = {e.account_code for e in txn.entries}
        assert journal.PLATFORM_FEE_REVENUE not in accounts
        assert accounts == {journal.AGENT_RECEIVABLE, journal.PUBLISHER_PAYABLE}

        by_account = {e.account_code: e.direction for e in txn.entries}
        assert by_account[journal.AGENT_RECEIVABLE] == DEBIT
        assert by_account[journal.PUBLISHER_PAYABLE] == CREDIT

    def test_confirming_collection_clears_the_receivable(self):
        txn = journal.confirm_collection(
            command_ref="col_1", amount_paise=500, agent_id="agent-a", batch_id="batch_1"
        )
        by_account = {e.account_code: e.direction for e in txn.entries}
        assert by_account[journal.GATEWAY_CLEARING] == DEBIT
        assert by_account[journal.AGENT_RECEIVABLE] == CREDIT

    def test_release_is_the_exact_mirror_of_reserve(self):
        args = dict(amount_paise=500, agent_id="agent-a", operator_id="op_1", offer_id="off_1")
        held = journal.reserve_authority(command_ref="r1", **args)
        freed = journal.release_reservation(command_ref="r2", **args)

        held_dirs = {e.account_code: e.direction for e in held.entries}
        freed_dirs = {e.account_code: e.direction for e in freed.entries}
        for account, direction in held_dirs.items():
            assert freed_dirs[account] != direction

    def test_a_reversal_mirrors_every_leg_and_names_the_original(self):
        original = journal.capture_usage(
            command_ref="cap_2",
            amount_paise=750,
            agent_id="agent-a",
            operator_id="op_1",
            merchant_id=None,
            commitment_id="cmt_2",
        )
        reversal = journal.reverse(
            command_ref="rev_1", original=original, reason="posted against the wrong agent"
        )

        assert reversal.reverses_txn_id == original.txn_id
        assert reversal.total_debits == original.total_debits
        assert "wrong agent" in reversal.description

        original_dirs = {e.account_code: e.direction for e in original.entries}
        for entry in reversal.entries:
            assert entry.direction != original_dirs[entry.account_code]


# ---------------------------------------------------------------------------
# Properties, over all inputs
# ---------------------------------------------------------------------------

# Realistic rupee amounts: 1 paisa to ₹1,00,000. Wide enough to catch overflow
# and rounding assumptions, bounded so the tests stay fast.
amounts = st.integers(min_value=1, max_value=10_000_000)


class TestAllocationProperties:
    """Refund allocation across an aggregate collection."""

    @given(
        total=amounts,
        commitments=st.lists(amounts, min_size=1, max_size=40),
    )
    @settings(max_examples=300, deadline=None)
    def test_allocation_always_sums_exactly(self, total, commitments):
        """No rounding drift, ever.

        A refund one paisa short of what was collected leaves a residue nobody
        can explain and nobody can clear. This is the property that has to hold
        for every possible split, not just tidy ones.
        """
        pool = sum(commitments)
        if total > pool:
            with pytest.raises(ValueError):
                allocate_refund(total_paise=total, commitment_amounts=commitments)
            return

        parts = allocate_refund(total_paise=total, commitment_amounts=commitments)
        assert sum(parts) == total
        assert len(parts) == len(commitments)
        assert all(isinstance(p, int) for p in parts)
        assert all(p >= 0 for p in parts)

    @given(commitments=st.lists(amounts, min_size=1, max_size=20))
    @settings(max_examples=200, deadline=None)
    def test_a_full_refund_returns_each_commitment_exactly(self, commitments):
        """Refunding the whole collection gives each commitment its own amount back.

        The one case where the answer is not a matter of apportionment policy,
        so getting it wrong would be unambiguous.
        """
        parts = allocate_refund(
            total_paise=sum(commitments), commitment_amounts=commitments
        )
        assert parts == commitments

    @given(
        total=amounts,
        commitments=st.lists(amounts, min_size=1, max_size=20),
    )
    @settings(max_examples=200, deadline=None)
    def test_no_commitment_is_allocated_more_than_it_contributed(self, total, commitments):
        """Otherwise a refund could over-credit one commitment and under-credit another."""
        if total > sum(commitments):
            return
        parts = allocate_refund(total_paise=total, commitment_amounts=commitments)
        for allocated, contributed in zip(parts, commitments, strict=True):
            assert allocated <= contributed

    @given(commitments=st.lists(amounts, min_size=1, max_size=10))
    @settings(max_examples=100, deadline=None)
    def test_allocation_is_deterministic(self, commitments):
        """Same inputs, same split — every time.

        Which commitment absorbs the rounding matters less than that it is the
        same one on a re-run: a reconciler comparing two computations of the
        same refund must not see them disagree.
        """
        total = max(1, sum(commitments) // 3)
        first = allocate_refund(total_paise=total, commitment_amounts=commitments)
        second = allocate_refund(total_paise=total, commitment_amounts=commitments)
        assert first == second


class TestBalanceProperties:
    @given(legs=st.lists(amounts, min_size=1, max_size=12))
    @settings(max_examples=200, deadline=None)
    def test_any_matched_set_of_legs_balances(self, legs):
        """Debits equal credits for any set of amounts, mirrored."""
        entries = [Entry(journal.AGENT_RECEIVABLE, DEBIT, amount) for amount in legs]
        entries += [Entry(journal.PUBLISHER_PAYABLE, CREDIT, amount) for amount in legs]

        txn = build_transaction(
            command_ref=f"cmd_{uuid.uuid4().hex}",
            txn_type=journal.CAPTURE_USAGE,
            description="property",
            entries=entries,
        )
        assert txn.total_debits == txn.total_credits == sum(legs)

    @given(debit=amounts, credit=amounts)
    @settings(max_examples=200, deadline=None)
    def test_mismatched_legs_never_build(self, debit, credit):
        entries = [
            Entry(journal.AGENT_RECEIVABLE, DEBIT, debit),
            Entry(journal.PUBLISHER_PAYABLE, CREDIT, credit),
        ]
        if debit == credit:
            assert build_transaction(
                command_ref=f"cmd_{uuid.uuid4().hex}",
                txn_type=journal.CAPTURE_USAGE,
                description="property",
                entries=entries,
            )
        else:
            with pytest.raises(UnbalancedTransaction):
                build_transaction(
                    command_ref=f"cmd_{uuid.uuid4().hex}",
                    txn_type=journal.CAPTURE_USAGE,
                    description="property",
                    entries=entries,
                )


# ---------------------------------------------------------------------------
# Posting to the ledger
# ---------------------------------------------------------------------------


def capture(ledger, *, command_ref, amount=500, agent_id="agent-a", commitment_id="cmt_1"):
    return ledger.post_journal(
        journal.capture_usage(
            command_ref=command_ref,
            amount_paise=amount,
            agent_id=agent_id,
            operator_id="op_1",
            merchant_id="mer_1",
            commitment_id=commitment_id,
        )
    )


class TestPosting:
    def test_the_chart_of_accounts_is_seeded(self, ledger):
        balance = ledger.trial_balance()
        codes = {a["accountCode"] for a in balance["accounts"]}
        assert codes == journal.ACCOUNT_CODES

    def test_seeding_twice_inserts_nothing_the_second_time(self, ledger):
        assert ledger.seed_accounts() == 0

    def test_a_posted_transaction_moves_both_accounts(self, ledger):
        capture(ledger, command_ref="capture:cmt_1")

        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 500
        assert ledger.account_balance(journal.PUBLISHER_PAYABLE) == 500

    def test_the_trial_balance_balances(self, ledger):
        capture(ledger, command_ref="capture:cmt_1", amount=500)
        capture(ledger, command_ref="capture:cmt_2", amount=750, commitment_id="cmt_2")
        ledger.post_journal(
            journal.confirm_collection(
                command_ref="collect:batch_1",
                amount_paise=1250,
                agent_id="agent-a",
                batch_id="batch_1",
            )
        )

        balance = ledger.trial_balance()
        assert balance["balanced"] is True
        assert balance["totalDebitsPaise"] == balance["totalCreditsPaise"]

    def test_a_replayed_command_posts_nothing(self, ledger):
        """The property that makes a retry after an unknown outcome safe."""
        first_id, first_posted = capture(ledger, command_ref="capture:cmt_1")
        second_id, second_posted = capture(ledger, command_ref="capture:cmt_1")

        assert first_posted is True
        assert second_posted is False
        assert second_id == first_id, "a replay must return the original transaction"

        # And crucially, no money moved twice.
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 500

    def test_many_replays_still_post_once(self, ledger):
        for _ in range(10):
            capture(ledger, command_ref="capture:cmt_1")
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 500

    def test_concurrent_replays_post_once(self, ledger):
        """Two retries arriving together must not both observe an absent row."""
        from concurrent.futures import ThreadPoolExecutor

        def attempt(_):
            try:
                return capture(ledger, command_ref="capture:cmt_race")[1]
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))

        assert sum(results) == 1, "exactly one attempt may post"
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 500

    def test_an_unbalanced_transaction_is_refused_at_post_time_too(self, ledger):
        """Two checks, not one.

        `build_transaction` already refuses this. Re-checking in `post_journal`
        means the invariant survives somebody constructing a Transaction
        directly — one check is a behaviour, two is an invariant.
        """
        rogue = journal.Transaction(
            txn_id="jtx_rogue",
            command_ref="rogue",
            txn_type=journal.CAPTURE_USAGE,
            description="hand-built",
            entries=(
                Entry(journal.AGENT_RECEIVABLE, DEBIT, 500),
                Entry(journal.PUBLISHER_PAYABLE, CREDIT, 400),
            ),
        )
        with pytest.raises(UnbalancedTransaction):
            ledger.post_journal(rogue)

        assert ledger.trial_balance()["totalDebitsPaise"] == 0

    def test_a_reversal_leaves_the_original_intact(self, ledger):
        """Corrections compensate; they never edit.

        The books have to show both what was believed and what turned out to
        be true, because the difference between those is the entire content of
        an audit trail.
        """
        original = journal.capture_usage(
            command_ref="capture:cmt_9",
            amount_paise=500,
            agent_id="agent-a",
            operator_id="op_1",
            merchant_id=None,
            commitment_id="cmt_9",
        )
        ledger.post_journal(original)
        ledger.post_journal(
            journal.reverse(
                command_ref="reverse:cmt_9", original=original, reason="wrong agent"
            )
        )

        # Net zero...
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 0
        # ...but BOTH transactions are still on the books.
        entries = ledger.journal_entries_for(commitment_id="cmt_9")
        assert len(entries) == 4
        assert any(e["reversesTxnId"] == original.txn_id for e in entries)

    def test_entries_can_be_traced_back_to_one_commitment(self, ledger):
        capture(ledger, command_ref="capture:cmt_1", commitment_id="cmt_1")
        capture(ledger, command_ref="capture:cmt_2", commitment_id="cmt_2")

        entries = ledger.journal_entries_for(commitment_id="cmt_1")
        assert len(entries) == 2
        assert {e["commitmentId"] for e in entries} == {"cmt_1"}


class TestReportedFiguresReconcile:
    """Every displayed aggregate must be recomputable from immutable records."""

    def test_collected_reconciles_between_the_summary_and_the_journal(self, ledger):
        """Two views of the same money must agree.

        `daily_summary` reads the commitment and batch status columns; the
        journal reads immutable postings. They are computed by completely
        different code paths, so agreement is meaningful — and a drift between
        them would mean the number a publisher reads is not the number the
        books hold.
        """
        settle_date = "2026-09-05"
        for index, amount in enumerate([500, 500, 250], start=1):
            offer_id = f"off_{index:020d}"
            ledger.insert_offer(
                {
                    "offerId": offer_id,
                    "agentId": "agent-a",
                    "resourceId": "market-report",
                    "resourceUrl": None,
                    "amountPaise": amount,
                    "asset": "INR",
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                    "nonce": f"n{index}",
                    "issuedAt": f"{settle_date}T09:00:00Z",
                    "expiresAt": f"{settle_date}T09:05:00Z",
                },
                f"sig{index}",
            )
            commitment = ledger.create_commitment(
                commitment_id=f"cmt_{index:020d}",
                offer_id=offer_id,
                agent_id="agent-a",
                resource_id="market-report",
                amount_paise=amount,
                asset="INR",
                mode="deferred",
                settle_date=settle_date,
            )
            ledger.post_journal(
                journal.capture_usage(
                    command_ref=f"capture:{commitment['commitmentId']}",
                    amount_paise=amount,
                    agent_id="agent-a",
                    operator_id=None,
                    merchant_id=None,
                    commitment_id=commitment["commitmentId"],
                )
            )

        summary = ledger.daily_summary(settle_date)

        # Accrued: the summary's committed total is the journal's receivable.
        assert summary["committedPaise"] == 1250
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 1250

        # Nothing collected yet, in either view.
        assert summary["collectedPaise"] == 0
        assert ledger.account_balance(journal.GATEWAY_CLEARING) == 0

        # Now collect, and check they move together.
        pending = ledger.pending_commitments(settle_date=settle_date)
        ledger.record_batch(
            batch_id="batch_1",
            agent_id="agent-a",
            settle_date=settle_date,
            commitment_ids=[c["commitment_id"] for c in pending],
            total_paise=1250,
            payment_link_id="plink_test",
            payment_link_url="https://example.invalid/x",
            status="created",
            razorpay_mode="mock",
        )
        ledger.mark_batch_paid(
            payment_link_id="plink_test",
            amount_paid_paise=1250,
            razorpay_payment_id="pay_test",
        )
        ledger.post_journal(
            journal.confirm_collection(
                command_ref="collect:batch_1",
                amount_paise=1250,
                agent_id="agent-a",
                batch_id="batch_1",
            )
        )

        after = ledger.daily_summary(settle_date)
        assert after["collectedPaise"] == 1250
        assert ledger.account_balance(journal.GATEWAY_CLEARING) == 1250
        # The receivable is cleared: the agent no longer owes it.
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 0
        assert ledger.trial_balance()["balanced"] is True

    def test_a_failed_gateway_operation_creates_no_collected_revenue(self, ledger):
        """The single most important invariant in the whole system.

        A batch that failed at the gateway posts nothing. Only
        `confirm_collection` — which runs on a signature-verified webhook —
        moves value into gateway clearing.
        """
        capture(ledger, command_ref="capture:cmt_1", amount=500)

        ledger.record_batch(
            batch_id="batch_failed",
            agent_id="agent-a",
            settle_date="2026-09-05",
            commitment_ids=[],
            total_paise=500,
            payment_link_id=None,
            payment_link_url=None,
            status="failed",
            razorpay_mode="mock",
            error_message="gateway said no",
        )

        assert ledger.account_balance(journal.GATEWAY_CLEARING) == 0
        assert ledger.account_balance(journal.AGENT_RECEIVABLE) == 500
        assert ledger.trial_balance()["balanced"] is True
