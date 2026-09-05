# ADR 0002 — Deferred collection, and owning the credit risk it creates

**Status:** Accepted · **Date:** 2026-09-05

## Context

Razorpay's Payment Links API rejects INR amounts below 100 subunits — ₹1.00 — which the official
documentation states and our own test-API call confirmed with
`amount: amount should be minimum 1.00 for INR.`

Agent API pricing wants to live well under that. A 50-paise fetch cannot be charged individually,
so per-request collection is not merely uneconomic at this price point; it is **impossible**.

Deferred, aggregated collection solves it: record a receivable per request, collect many at once
later. But it creates a liability that must be stated plainly — **the publisher has served the
content before any money moved.**

## Decision

Defer collection and **model the resulting credit risk explicitly** rather than leaving it
implicit.

1. A fulfilled request produces a **receivable**, never a payment.
2. Receivables aggregate by agent and settlement date into a **collection batch**.
3. A batch's existence is an *invoice*. Only a signature-verified gateway confirmation moves it to
   collected.
4. Exposure — accrued minus collected — is a first-class reported number, on the dashboard, in the
   digest, and in the API.
5. Exposure is **bounded before it is incurred** by reserved authority (ADR 0001) and per-agent
   daily caps enforced inside the booking write.
6. Failed collection produces overdue exposure and, past a threshold, suspends further
   authorization. Write-off requires an authenticated operator action and posts a compensating
   journal entry — nothing is ever deleted to make the books look better.

## The argument this project explicitly does **not** make

**Batching does not reduce percentage fees.** Two percent of a hundred ₹5 charges is two percent of
one ₹500 charge. An early version of the cost model here reported a saving that was a rounding
artifact; it was removed, and a test pins the honest version so it cannot creep back.

What batching actually buys:

| Benefit | Real? |
| --- | --- |
| Makes sub-₹1 pricing possible at all | **Yes** — this is the whole argument |
| Fewer gateway API calls (60 requests → 5 charges) | **Yes**, observed |
| Avoids the observed `429 Too many requests` on back-to-back link creation | **Yes**, observed |
| Fewer reconciliation rows to manage | Yes |
| Amortises any *fixed* per-transaction fee | Yes, where one exists |
| Reduces percentage fees | **No** |

## Consequences

**Good.** A price point that could not otherwise exist. Far fewer gateway calls. A single
reconciliation unit per agent per day.

**Costs.**

- **Real credit risk.** Content is gone; the money may not arrive. This is the honest cost and it
  is not engineered away.
- Revenue recognition is genuinely harder — hence the double-entry journal in ADR 0004.
- Collection failure is a routine path, not an edge case, so refunds, retries, delinquency, and
  reconciliation all become required product surface rather than nice-to-haves.
- A Payment Link may need **a human to open a page**, which is absurd in a machine-to-machine
  flow. Simulated Reserve Pay removes the page but not the ₹1 floor — see ADR 0002's companion note
  in the README. Both are needed, which is why the mandate instrument settles a batch rather than
  replacing batching.

**Scope discipline.** The ₹1 finding is scoped to the **Payment Links API**. It is not generalised
to every Razorpay product, to UPI, or to NPCI rails, because no source supports that.
