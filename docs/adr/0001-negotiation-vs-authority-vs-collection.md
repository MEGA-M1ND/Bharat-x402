# ADR 0001 — Separate negotiation, authority, and collection

**Status:** Accepted · **Date:** 2026-09-05

## Context

The project began as an x402 facilitator: quote a price, verify a signature, book a commitment,
batch the commitments, create a Payment Link. That flow works and is tested, but it silently
answers three unrelated questions with one mechanism.

1. **Negotiation** — what does this resource cost, and did the caller agree to that price?
2. **Authority** — is this caller allowed to spend anyone's money, and how much?
3. **Collection** — how do rupees actually move, and did they?

The original design answered (1) properly with x402, answered (3) with Payment Links, and **did not
answer (2) at all.** A pseudonymous key signed "I owe ₹5" and the content was served. The signature
proves the key agreed; it says nothing about whether anything stands behind the promise.

This was visible in the vocabulary before it was visible in the code. `commitments.status =
'settled'` meant "assigned to a batch". The dashboard's headline tile said "earned" over a number
that was purely accrued. The digest summed *created* Payment Links into a variable named
`collected`. Each of those is the same missing distinction leaking into a different surface.

## Decision

Model the three as separate concerns with separate storage, separate failure modes, and separate
words.

- **Negotiation** stays exactly as it is: x402, quotes, Ed25519 acceptance. It is the part that
  already worked.
- **Authority** becomes a first-class subsystem: operators, consents with limits in integer paise,
  and reservations held atomically against an authority balance *before* content is released.
- **Collection** keeps its own lifecycle, its own confirmation evidence, and its own failure
  states, and is never inferred from the other two.

The gate moves. Content is released against **reserved authority**, not against a signature.

## Consequences

**Good.**

- "Can this agent pay?" and "did this agent pay?" stop being the same question.
- Credit exposure becomes a number on the dashboard instead of an unstated assumption.
- The three concerns fail independently: a collection failure no longer implies an authorization
  bug, and vice versa.
- The vocabulary becomes testable — `tests/test_terminology.py` fails the build if "earned" comes
  back over an accrued figure.

**Costs.**

- Three new entity groups and three migrations. Substantially more schema than a demo needs.
- A request now does more work before the handler runs: consent check plus an atomic reservation.
- Backwards compatibility requires a deliberately unsafe demo mode (`DEMO_UNSAFE_TOFU`) so the
  public demo keeps working without operator onboarding. An explicitly named unsafe flag with a
  startup warning is better than a quietly permissive default.

**Rejected alternatives.**

- *Leave authority to the agent.* The agent kit already enforces a budget, and that is worth
  nothing to the facilitator: the client is the party whose behaviour is in question, and anyone
  can write a different client.
- *Require prepayment, removing credit risk entirely.* This kills the sub-₹1 price point, which is
  the project's actual contribution. Prefunded authority is offered as one backing type rather than
  imposed as the only one.
- *Trust the publisher to check authority.* It has no ledger, no cross-publisher view of an agent's
  spend, and no relationship with the operator.
