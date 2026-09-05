# ADR 0004 — Integer paise, double-entry, and immutable postings

**Status:** Accepted · **Date:** 2026-09-05

## Context

The system now has to represent authority, reservations, receivables, collections, gateway fees,
refunds, publisher payables, and bad debt. Single-entry status columns were adequate while there
was one lifecycle (`offer → commitment → batch`). They are not adequate for a refund that must be
allocated across an aggregate collection, or for a write-off that has to leave an audit trail.

Two failure modes drive this decision:

1. **Floating-point money.** A single float anywhere in a monetary path silently loses precision
   and the error compounds across aggregation.
2. **Destructive correction.** "Fixing" a wrong balance by updating a row destroys the evidence of
   what happened, which is precisely what an auditor needs and precisely what a reconciler needs.

## Decision

### Integer paise, everywhere

Money is `INTEGER` paise in the database, `int` in Python, and a **string of integer paise** on the
wire — mirroring how x402 carries USDC atomic units. ₹5.00 is `"500"`.

No float touches a monetary value anywhere in the codebase. Rupee formatting happens only at
display time. A test asserts it.

Division — refund allocation across a batch — uses integer arithmetic with an explicit remainder
policy, so allocations sum **exactly** to the original. No rounding drift is tolerated.

### Double-entry journal

Every money-changing operation posts a `journal_transaction` with balanced `journal_entries`.

Accounts: operator reserved authority, agent receivable, gateway clearing, publisher payable,
platform fee revenue, gateway fee expense, refund liability, bad-debt expense.

### Postings are immutable

Nothing in the journal is ever updated or deleted. An error is corrected by posting a
**compensating transaction** that references the original. The books show both what was thought to
be true and what turned out to be true — which is the entire point.

### Idempotency by command reference

Each journal transaction carries a command/idempotency reference. A replayed command finds the
existing transaction and creates **no** additional financial movement. This is what makes retries
after an unknown gateway outcome safe.

## Invariants, enforced by tests

| Invariant | Why it matters |
| --- | --- |
| Debits equal credits, per transaction | The definition of balanced books |
| A replayed command creates no second movement | Retries are routine; double-charging is the worst outcome a payment system has |
| Captured usage never exceeds authorized/reserved value | The authority model would otherwise be decorative |
| Collection never exceeds outstanding receivables | Prevents collecting money nobody owes |
| Refund never exceeds collected eligible value | Prevents refunding money never received |
| Publisher payable never exceeds net collected value | Prevents promising a publisher money that did not arrive |
| Failed gateway operations create no collected revenue | The single most important one |
| Every displayed aggregate recomputes from immutable records | A dashboard that cannot be re-derived is a rumour |

Property-based tests generate operation sequences and assert the invariants hold across all of
them, rather than only on the paths someone thought to write by hand.

## Consequences

**Good.** Correctness is checkable rather than asserted. Reconciliation has something to reconcile
*against*. Refunds and write-offs become ordinary operations instead of surgery. Historic states
are reconstructible.

**Costs.**

- Substantially more writes per operation, and more schema.
- The existing `commitments`/`batches` status columns now duplicate information the journal also
  holds. They are kept — reporting and the existing tests depend on them — which means a
  consistency test is required to prove the two views agree. That test is itself valuable: it is
  what would catch a drift between the summary a publisher reads and the books underneath it.
- Real-world accounting semantics (tax points, GST, TDS, revenue recognition timing) are **not**
  modelled. The account names borrow the vocabulary without claiming the compliance. Documented as
  an assumption rather than implied by the structure.
