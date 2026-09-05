"""Double-entry journal: what happened to the money, in what order.

WHY THE STATUS COLUMNS ARE NOT ENOUGH
-------------------------------------
`commitments.status` and `batches.status` answer "where is this commitment?".
They cannot answer "what happened to the money, and does it add up?" — and the
operations Phase 5 needs answer only to the second question:

  * A refund allocated across an aggregate collection touches several
    commitments and a batch, and must sum back exactly.
  * A write-off has to leave a trail showing what was believed and when it
    changed.
  * A gateway call whose outcome is unknown must be safe to retry, which means
    the retry has to be able to find out whether the first attempt posted
    anything.

A status column can represent the *current* state. It cannot represent the
sequence of states, the amounts that moved between them, or the fact that two
different corrections were applied a week apart.

TWO RULES
---------
1. **Debits equal credits, per transaction.** Checked in `build_transaction`
   before anything is written. An unbalanced posting is a programming error
   and is refused, not stored and reported later.

2. **Nothing is ever updated or deleted.** An error is corrected by posting a
   *compensating* transaction that references the original. The books then
   show both what was believed and what turned out to be true, which is what
   an auditor and a reconciler both need. Editing the original destroys the
   only evidence that the mistake happened.

IDEMPOTENCY
-----------
Every transaction carries a `command_ref`, and that column is UNIQUE. A
replayed command finds the existing row and posts nothing — enforced by the
database, not by application logic remembering to check. This is what makes a
retry after an unknown gateway outcome safe: the retry cannot double-post,
whatever it believes about the first attempt.

SIGN CONVENTION
---------------
`amount_paise` is always **positive**; `direction` carries the sign. Signed
amounts *plus* a direction column would let the same fact be written two ways,
and a balance check would then have to guess which convention each row used.

WHAT THIS DOES NOT CLAIM
------------------------
Real-world accounting semantics — revenue recognition timing, GST tax points,
TDS, statutory reporting — are **not** modelled. The account names borrow the
vocabulary of double-entry bookkeeping because the structure genuinely is
double-entry; they do not claim the compliance that would come with it. Stated
here rather than implied by the presence of an account called "tax payable".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

DEBIT = "debit"
CREDIT = "credit"

# ---------------------------------------------------------- chart of accounts
#
# Deliberately small. Every account here is one this project actually posts to;
# a chart full of accounts nothing touches is a chart nobody trusts.

ASSET = "asset"
LIABILITY = "liability"
EQUITY = "equity"
REVENUE = "revenue"
EXPENSE = "expense"

# (code, name, type, normal balance)
CHART_OF_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    # -- assets ------------------------------------------------------------
    (
        "1100",
        "Agent receivable",
        ASSET,
        DEBIT,
    ),
    (
        "1200",
        "Gateway clearing",
        ASSET,
        DEBIT,
    ),
    # -- liabilities -------------------------------------------------------
    (
        "2100",
        "Operator reserved authority",
        LIABILITY,
        CREDIT,
    ),
    (
        "2200",
        "Publisher payable",
        LIABILITY,
        CREDIT,
    ),
    (
        "2300",
        "Refund liability",
        LIABILITY,
        CREDIT,
    ),
    (
        "2400",
        "Tax payable",
        LIABILITY,
        CREDIT,
    ),
    # -- revenue and expense ----------------------------------------------
    (
        "4100",
        "Platform fee revenue",
        REVENUE,
        CREDIT,
    ),
    (
        "5100",
        "Gateway fee expense",
        EXPENSE,
        DEBIT,
    ),
    (
        "5200",
        "Bad debt expense",
        EXPENSE,
        DEBIT,
    ),
)

AGENT_RECEIVABLE = "1100"
GATEWAY_CLEARING = "1200"
RESERVED_AUTHORITY = "2100"
PUBLISHER_PAYABLE = "2200"
REFUND_LIABILITY = "2300"
TAX_PAYABLE = "2400"
PLATFORM_FEE_REVENUE = "4100"
GATEWAY_FEE_EXPENSE = "5100"
BAD_DEBT_EXPENSE = "5200"

ACCOUNT_CODES = frozenset(code for code, _, _, _ in CHART_OF_ACCOUNTS)

# Transaction types. Named for the economic event, not the HTTP endpoint that
# happened to trigger it — `capture_usage` stays meaningful if /settle is ever
# renamed, and it is the thing an accountant would recognise.
RESERVE_AUTHORITY = "reserve_authority"
RELEASE_RESERVATION = "release_reservation"
CAPTURE_USAGE = "capture_usage"
INITIATE_COLLECTION = "initiate_collection"
CONFIRM_COLLECTION = "confirm_collection"
RECORD_GATEWAY_FEE = "record_gateway_fee"
INITIATE_REFUND = "initiate_refund"
CONFIRM_REFUND = "confirm_refund"
WRITE_OFF_BAD_DEBT = "write_off_bad_debt"
REVERSAL = "reversal"


class UnbalancedTransaction(Exception):
    """Debits did not equal credits.

    A programming error, not a runtime condition — which is why it is raised
    at build time and never stored. A journal that can hold an unbalanced
    transaction is not a journal.
    """


@dataclass(frozen=True)
class Entry:
    """One leg of a transaction.

    `amount_paise` is positive; `direction` carries the sign.
    """

    account_code: str
    direction: str
    amount_paise: int
    agent_id: str | None = None
    operator_id: str | None = None
    merchant_id: str | None = None
    commitment_id: str | None = None
    batch_id: str | None = None

    def __post_init__(self) -> None:
        if self.account_code not in ACCOUNT_CODES:
            raise ValueError(
                f"unknown account {self.account_code!r}; "
                f"known: {', '.join(sorted(ACCOUNT_CODES))}"
            )
        if self.direction not in (DEBIT, CREDIT):
            raise ValueError(f"direction must be {DEBIT!r} or {CREDIT!r}, got {self.direction!r}")
        if not isinstance(self.amount_paise, int) or isinstance(self.amount_paise, bool):
            # bool is an int subclass, and `True` as an amount is never intended.
            raise TypeError(
                f"amount must be an integer number of paise, got {type(self.amount_paise).__name__}"
            )
        if self.amount_paise <= 0:
            raise ValueError(
                f"amount must be positive; the direction carries the sign "
                f"(got {self.amount_paise})"
            )


@dataclass(frozen=True)
class Transaction:
    """A balanced set of entries, ready to post."""

    txn_id: str
    command_ref: str
    txn_type: str
    description: str
    entries: tuple[Entry, ...] = field(default_factory=tuple)
    reverses_txn_id: str | None = None

    @property
    def total_debits(self) -> int:
        return sum(e.amount_paise for e in self.entries if e.direction == DEBIT)

    @property
    def total_credits(self) -> int:
        return sum(e.amount_paise for e in self.entries if e.direction == CREDIT)


def build_transaction(
    *,
    command_ref: str,
    txn_type: str,
    description: str,
    entries: list[Entry],
    reverses_txn_id: str | None = None,
) -> Transaction:
    """Validates entries and packages them for posting.

    The balance check happens HERE, before any write, so an unbalanced
    transaction is a caught programming error rather than a corrupt row
    somebody finds during a reconciliation three weeks later.

    Args:
        command_ref: Idempotency key. Must be deterministic for the operation
            it represents — a UUID generated per attempt would make every
            retry a new transaction, which is the opposite of what this is for.
        txn_type: One of the module's transaction-type constants.
        description: Human-readable, for the audit trail.
        entries: At least two legs.
        reverses_txn_id: Set when this compensates an earlier transaction.

    Returns:
        A `Transaction`.

    Raises:
        UnbalancedTransaction: If debits do not equal credits, or there are
            fewer than two entries.
    """
    if len(entries) < 2:
        raise UnbalancedTransaction(
            f"a double-entry transaction needs at least two legs, got {len(entries)}"
        )

    debits = sum(e.amount_paise for e in entries if e.direction == DEBIT)
    credits = sum(e.amount_paise for e in entries if e.direction == CREDIT)
    if debits != credits:
        raise UnbalancedTransaction(
            f"{txn_type}: debits {debits} != credits {credits} "
            f"(difference {debits - credits} paise)"
        )

    return Transaction(
        txn_id=f"jtx_{uuid.uuid4().hex[:20]}",
        command_ref=command_ref,
        txn_type=txn_type,
        description=description,
        entries=tuple(entries),
        reverses_txn_id=reverses_txn_id,
    )


# ------------------------------------------------------- the economic events
#
# Each builder below is one economic event, expressed as the legs it moves.
# They are pure: no database, no clock, no configuration — which makes each one
# testable by reading it, and makes "is this the right accounting?" a question
# you can answer without running anything.


def reserve_authority(
    *, command_ref: str, amount_paise: int, agent_id: str, operator_id: str, offer_id: str
) -> Transaction:
    """An amount is held against an operator's authority.

    Both legs are the platform's own view of an obligation it has taken on, so
    this is deliberately *not* revenue and *not* a receivable: nothing is owed
    yet, because no content has been delivered. It records that authority is
    encumbered.
    """
    return build_transaction(
        command_ref=command_ref,
        txn_type=RESERVE_AUTHORITY,
        description=f"Reserved {amount_paise} paise for offer {offer_id}",
        entries=[
            Entry(RESERVED_AUTHORITY, DEBIT, amount_paise, agent_id, operator_id),
            Entry(GATEWAY_CLEARING, CREDIT, amount_paise, agent_id, operator_id),
        ],
    )


def release_reservation(
    *, command_ref: str, amount_paise: int, agent_id: str, operator_id: str, offer_id: str
) -> Transaction:
    """Fulfillment failed; the hold is returned. The exact reverse of the above."""
    return build_transaction(
        command_ref=command_ref,
        txn_type=RELEASE_RESERVATION,
        description=f"Released {amount_paise} paise held for offer {offer_id}",
        entries=[
            Entry(GATEWAY_CLEARING, DEBIT, amount_paise, agent_id, operator_id),
            Entry(RESERVED_AUTHORITY, CREDIT, amount_paise, agent_id, operator_id),
        ],
    )


def capture_usage(
    *,
    command_ref: str,
    amount_paise: int,
    agent_id: str,
    operator_id: str | None,
    merchant_id: str | None,
    commitment_id: str,
) -> Transaction:
    """Content was delivered, so a receivable exists and the publisher is owed.

    This is the posting that most needs to be right, and the one most easily
    written wrong. It debits **agent receivable** — an asset, because the agent
    now owes us — and credits **publisher payable** — a liability, because we
    now owe the publisher.

    Note what it does NOT credit: revenue. No money has arrived. Crediting a
    revenue account here is precisely the mistake the whole project argues
    against, expressed in accounts instead of in a dashboard tile.
    """
    return build_transaction(
        command_ref=command_ref,
        txn_type=CAPTURE_USAGE,
        description=f"Captured {amount_paise} paise of fulfilled usage ({commitment_id})",
        entries=[
            Entry(
                AGENT_RECEIVABLE,
                DEBIT,
                amount_paise,
                agent_id,
                operator_id,
                merchant_id,
                commitment_id,
            ),
            Entry(
                PUBLISHER_PAYABLE,
                CREDIT,
                amount_paise,
                agent_id,
                operator_id,
                merchant_id,
                commitment_id,
            ),
        ],
    )


def confirm_collection(
    *,
    command_ref: str,
    amount_paise: int,
    agent_id: str,
    batch_id: str,
    merchant_id: str | None = None,
) -> Transaction:
    """The gateway confirmed money arrived for a batch.

    Debits gateway clearing (we now hold value) and credits agent receivable
    (the agent no longer owes it). Only THIS transaction converts a receivable
    into held value — creating the Payment Link posts nothing, because asking
    for money is not receiving it.
    """
    return build_transaction(
        command_ref=command_ref,
        txn_type=CONFIRM_COLLECTION,
        description=f"Collection confirmed for batch {batch_id}: {amount_paise} paise",
        entries=[
            Entry(GATEWAY_CLEARING, DEBIT, amount_paise, agent_id, merchant_id=merchant_id,
                  batch_id=batch_id),
            Entry(AGENT_RECEIVABLE, CREDIT, amount_paise, agent_id, merchant_id=merchant_id,
                  batch_id=batch_id),
        ],
    )


def record_gateway_fee(
    *, command_ref: str, fee_paise: int, batch_id: str, merchant_id: str | None = None
) -> Transaction:
    """The gateway's cut, taken out of what it holds for us."""
    return build_transaction(
        command_ref=command_ref,
        txn_type=RECORD_GATEWAY_FEE,
        description=f"Gateway fee on batch {batch_id}: {fee_paise} paise",
        entries=[
            Entry(GATEWAY_FEE_EXPENSE, DEBIT, fee_paise, merchant_id=merchant_id,
                  batch_id=batch_id),
            Entry(GATEWAY_CLEARING, CREDIT, fee_paise, merchant_id=merchant_id,
                  batch_id=batch_id),
        ],
    )


def initiate_refund(
    *,
    command_ref: str,
    amount_paise: int,
    agent_id: str,
    commitment_id: str,
    merchant_id: str | None = None,
) -> Transaction:
    """A refund is owed but has not moved.

    Two states, not one: owing a refund and having paid it are different
    facts, and a system that merges them cannot answer "what refunds are
    outstanding?" — which is exactly what someone chasing a stuck refund asks.
    The publisher's payable is reduced here, because the value is no longer
    theirs to be paid.
    """
    return build_transaction(
        command_ref=command_ref,
        txn_type=INITIATE_REFUND,
        description=f"Refund initiated for {commitment_id}: {amount_paise} paise",
        entries=[
            Entry(PUBLISHER_PAYABLE, DEBIT, amount_paise, agent_id,
                  merchant_id=merchant_id, commitment_id=commitment_id),
            Entry(REFUND_LIABILITY, CREDIT, amount_paise, agent_id,
                  merchant_id=merchant_id, commitment_id=commitment_id),
        ],
    )


def confirm_refund(
    *,
    command_ref: str,
    amount_paise: int,
    agent_id: str,
    commitment_id: str,
    merchant_id: str | None = None,
) -> Transaction:
    """The refund actually moved, clearing the liability."""
    return build_transaction(
        command_ref=command_ref,
        txn_type=CONFIRM_REFUND,
        description=f"Refund confirmed for {commitment_id}: {amount_paise} paise",
        entries=[
            Entry(REFUND_LIABILITY, DEBIT, amount_paise, agent_id,
                  merchant_id=merchant_id, commitment_id=commitment_id),
            Entry(GATEWAY_CLEARING, CREDIT, amount_paise, agent_id,
                  merchant_id=merchant_id, commitment_id=commitment_id),
        ],
    )


def write_off_bad_debt(
    *, command_ref: str, amount_paise: int, agent_id: str, commitment_id: str
) -> Transaction:
    """A receivable will not be collected.

    Recognises the loss as an expense and removes the asset. Requires an
    authenticated operator action at the API layer — a system that can write
    off its own debts silently has no receivables, only optimism.

    Note that the publisher payable is NOT reduced here. Whether the publisher
    still gets paid for content that was delivered but never collected is a
    commercial decision, not an accounting one, and this project does not
    presume it. Documented rather than quietly chosen.
    """
    return build_transaction(
        command_ref=command_ref,
        txn_type=WRITE_OFF_BAD_DEBT,
        description=f"Wrote off {amount_paise} paise: {commitment_id}",
        entries=[
            Entry(BAD_DEBT_EXPENSE, DEBIT, amount_paise, agent_id, commitment_id=commitment_id),
            Entry(AGENT_RECEIVABLE, CREDIT, amount_paise, agent_id, commitment_id=commitment_id),
        ],
    )


def reverse(*, command_ref: str, original: Transaction, reason: str) -> Transaction:
    """Compensates an earlier transaction by mirroring every leg.

    The correction mechanism, and the only one. There is no "fix the row"
    path, because a corrected row cannot be distinguished from a row that was
    always right — and the difference between those two is the entire content
    of an audit trail.

    Args:
        command_ref: Idempotency key for the reversal itself.
        original: The transaction being compensated.
        reason: Why. Recorded verbatim.

    Returns:
        A balanced transaction with every leg's direction flipped.
    """
    mirrored = [
        Entry(
            account_code=e.account_code,
            direction=CREDIT if e.direction == DEBIT else DEBIT,
            amount_paise=e.amount_paise,
            agent_id=e.agent_id,
            operator_id=e.operator_id,
            merchant_id=e.merchant_id,
            commitment_id=e.commitment_id,
            batch_id=e.batch_id,
        )
        for e in original.entries
    ]
    return build_transaction(
        command_ref=command_ref,
        txn_type=REVERSAL,
        description=f"Reversal of {original.txn_id} ({original.txn_type}): {reason}",
        entries=mirrored,
        reverses_txn_id=original.txn_id,
    )


def allocate_refund(*, total_paise: int, commitment_amounts: list[int]) -> list[int]:
    """Splits a refund across the commitments in a collection, exactly.

    Integer arithmetic with an explicit remainder policy. The allocations sum
    to `total_paise` **exactly** — no rounding drift is tolerated, because a
    refund that is one paisa short of what was collected leaves a balance
    nobody can explain and nobody can clear.

    Largest-remainder: allocate the floor of each proportional share, then give
    the leftover paise one at a time to the commitments with the largest
    fractional parts. That is the standard apportionment method and it is
    deterministic, which matters more than which specific commitment absorbs
    the rounding.

    Args:
        total_paise: The refund, in integer paise.
        commitment_amounts: What each commitment contributed, in order.

    Returns:
        One allocation per commitment, in the same order, summing exactly to
        `total_paise`.

    Raises:
        ValueError: If the refund exceeds the total collected, or the inputs
            are empty or non-positive.
    """
    if not commitment_amounts:
        raise ValueError("cannot allocate a refund across zero commitments")
    if total_paise <= 0:
        raise ValueError(f"refund must be positive, got {total_paise}")

    pool = sum(commitment_amounts)
    if total_paise > pool:
        raise ValueError(
            f"refund of {total_paise} paise exceeds the {pool} paise collected"
        )

    # Floor share plus the numerator of the remainder, per commitment.
    floors = [(total_paise * amount) // pool for amount in commitment_amounts]
    remainders = [(total_paise * amount) % pool for amount in commitment_amounts]

    leftover = total_paise - sum(floors)
    # Largest remainder first; ties broken by index so the result is stable.
    order = sorted(range(len(commitment_amounts)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        floors[i] += 1

    assert sum(floors) == total_paise, "allocation must sum exactly"
    return floors
