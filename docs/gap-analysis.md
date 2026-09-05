# Gap analysis

Written before the Phase 1–4 changes, and kept current as they landed. The point of this document
is to be the place a sceptical reviewer looks first: what is actually demonstrated, what is
staged, and what is only asserted.

---

## 1. What this project proves

These are demonstrated by code that runs, with tests that would fail if the claim stopped being
true.

| Claim | How it is proven |
| --- | --- |
| An unmodified `@x402/express` publisher can be settled by a non-blockchain, INR facilitator | The stock middleware runs against our `/supported`, `/verify`, `/settle`. Only `scheme`, `network`, and facilitator URL differ. Smoke test asserts the 402 body shape. |
| A quote can be priced below any gateway's minimum and still be served | `/premium/api-call` is 50 paise. The request completes; no gateway call happens at request time. |
| An agent's acceptance is **not forgeable by the facilitator** | Ed25519. The facilitator stores only public keys. `test_agent_keys.py` covers the algorithm-downgrade and agent-relabelling attacks. |
| Accrual and collection are **separate states** | `commitments.status` and `batches.status` are distinct columns; `daily_summary` reports `committedPaise` and `collectedPaise` separately. |
| The same offer cannot be spent twice, across processes | Conditional `UPDATE ... WHERE status='open'` plus `UNIQUE(offer_id)`. No process-local lock is load-bearing. Proven on real Postgres in CI. |
| A duplicate webhook does not double-count | Primary-key claim in `webhook_events`, not a read-then-check. |
| The webhook handler fails closed | No `RAZORPAY_WEBHOOK_SECRET` means every delivery is refused. Tested. |
| Money never touches a float | Integer paise throughout; a test asserts it. |
| The ledger code survives the SQLite → Postgres port | The entire suite runs against both engines in CI. |
| An agent's own budget is enforced in code, not in a prompt | `X402Client.pay_and_fetch` refuses before any HTTP happens; `test_agent_kit.py` covers it. |
| **(Phase 2)** A revoked or expired credential cannot authorize new activity | `test_operators_and_consent.py` |
| **(Phase 2)** One tenant cannot read another's ledger, economics, or audit trail | Tenant-isolation tests over every control-plane endpoint |
| **(Phase 3)** Two concurrent requests cannot overspend one authority balance | Atomic conditional `UPDATE` on `authority_accounts`; concurrency test on both engines |
| **(Phase 3)** Failed fulfillment releases the reservation; success captures exactly once | `test_reservations.py` |
| **(Phase 4)** Every money-changing operation posts balanced debits and credits | `test_journal.py`, including a property-based check over generated operation sequences |

## 2. What this project simulates

Staged, clearly labelled, and never presented as an integration.

| Simulated | What is real about it | What is not |
| --- | --- | --- |
| **Razorpay in `MOCK_RAZORPAY` mode** (the default) | Response shapes, error strings, and the ₹1 rejection are copied from real observed test-API behaviour | No network call happens; identifiers are fabricated |
| **UPI Reserve Pay** (`SETTLEMENT_INSTRUMENT=reserve_pay`) | Models the documented block/debit shape and the published ₹10,000 / 90-day limits | There is no NPCI or bank integration. No public API was available — activation is gated behind a Razorpay support request. Refuses to run outside `MOCK_RAZORPAY`. |
| **Reserved funds authority** (Phase 3, `simulated_reserve`) | The *domain* behaviour — atomic reservation, capture, release, and the impossibility of overspending — is real code with real concurrency tests | No money is held anywhere. It is a balance in our own table, not a bank block. |
| **Prefunded balances** (Phase 3) | Same: the accounting is real | Funding is a test-mode control-plane call, not a received payment |
| **Publisher payout** (Phase 4) | The payable is computed and journalled | Nothing is ever transferred to anyone |
| **Operator identity** (Phase 2) | Hashed API keys, scopes, rotation, revocation, tenant isolation — all real | No KYC, no legal identity, no relationship to a real business entity |

## 3. What this project does **not** prove

Stated because omitting them would be the dishonest part.

- **That anyone will pay.** No agent operator has committed to this. The demand side is assumed.
- **That the credit risk is manageable.** Deferred collection means the publisher has been given
  content before money moved. Phase 3 reduces this to a modelled exposure with limits; it does not
  eliminate it, and no real-world default rate informs the numbers.
- **That the economics work at scale.** The fee model is configuration. Real interchange,
  platform fees, GST, and TDS are not modelled.
- **That this is compliant.** Aggregating collections on behalf of publishers is regulated
  activity in India (payment aggregator licensing). This project does not address it. See
  [product-proposal.md](product-proposal.md) for the open questions.
- **That Ed25519 identifies anybody.** It proves key continuity. Under trust-on-first-use it
  proves *only* that the same key came back — which is why Phase 2 adds operators.
- **Availability, durability, or latency under load.** No benchmark has been run, so no SLO
  attainment is claimed — only targets.
- **That a real UPI Reserve Pay integration would work as modelled.** No API access.

## 4. Statements that needed correcting

Found during the survey. All corrected in Phase 1; `tests/test_terminology.py` fails the build if
any of them return.

| Was | Problem | Now |
| --- | --- | --- |
| "Cloudflare's x402 protocol" (`docs/architecture.md`, `public/index.html`, `server.js`) | **Factually wrong.** Coinbase authored x402. Cloudflare co-founded the x402 Foundation with them and separately ships Pay Per Crawl, which is not x402. | x402 is Coinbase-authored, Linux Foundation-governed; Cloudflare motivates the use case and is optional |
| Dashboard tile: **"earned"**, fed by `summary.totalPaise` | `totalPaise` is *committed*, not collected. The headline number on the publisher's dashboard overstated revenue. | Split into **accrued** and **collected** tiles, with outstanding exposure shown separately |
| "Paid ₹X" on a completed run (`app.js`) | Only a commitment existed | "Committed ₹X — not yet collected" |
| "paid requests" tile label | Same conflation | "authorized requests" |
| "Razorpay's ₹1.00 floor" (unqualified, README) | Generalised a Payment Links observation to all of Razorpay | "The Razorpay **Payment Links** API rejects INR amounts below ₹1.00" — with the official doc cited alongside the observed error string |
| "Razorpay retries until it gets a 2xx" | Unbounded; the real policy is bounded | "retries with exponential backoff for 24 hours, then disables the webhook" |
| "settlement receipt" for a commitment id | A commitment is not a settlement | "commitment receipt — a receivable, not a payment" |
| `commitments.status = 'settled'` | Means "assigned to a batch", reads as "money arrived" | Documented explicitly; the term **collected** is reserved for gateway-confirmed money |

## 5. Which guarantees are cryptographic

True because of mathematics, assuming the primitives hold and keys are not stolen.

- An acceptance carries an **Ed25519** signature over canonical JSON. The facilitator holds only
  the public key and therefore **cannot** produce one. Tamper-evident and non-repudiable *with
  respect to the key*.
- The facilitator, **not the payload**, selects the verification algorithm. Attacker-chosen
  algorithm agility (the JWT `alg` class of bug) is structurally unavailable.
- The signing key is looked up by the agent id on the **stored offer**, never the one in the
  payload — so an attacker cannot relabel themselves onto a weaker path.
- Webhook authenticity is **HMAC-SHA256 over the raw body** under a shared secret with Razorpay.
- API credentials are stored as **hashes** (Phase 2). A database disclosure does not yield usable
  keys.
- Constant-time comparison everywhere a secret is compared.

**What none of this gives you:** proof of funds, proof of legal identity, or proof that anyone
paid. A signature over "I accept a charge of ₹5" is exactly that and nothing more.

## 6. Which guarantees are financial

True because of database constraints and the accounting model — enforced, not hoped for.

- **One offer yields at most one commitment.** `UNIQUE(offer_id)` plus a conditional `UPDATE`.
- **Capture never exceeds the reserved amount** (Phase 3).
- **Concurrent requests cannot overspend one authority** — the check is inside the write.
- **Collection never exceeds outstanding receivables**; **refunds never exceed eligible collected
  value** (Phases 4–5).
- **Every money-changing operation is a balanced journal transaction**, and journal rows are
  immutable — errors are compensated, never overwritten (Phase 4).
- **Every displayed aggregate recomputes from immutable records.** Tested.
- Money is **integer paise** end to end.

**What none of this gives you:** any assurance that the money is collectable. The ledger can be
perfectly consistent about a debt nobody ever pays.

## 7. Which guarantees are merely product assumptions

Believed, reasonable, and unproven. Listed so they are not mistaken for the two categories above.

- That publishers **want** per-request agent revenue more than they want to block crawlers.
- That agent operators will **accept a consent model** with pre-set limits.
- That deferred collection is **commercially acceptable** to a publisher who has already served
  the content.
- That daily batching is the **right aggregation window** — it is a configured default, not an
  optimum derived from data.
- That a ₹0.50 price point is **meaningful** to anybody. It is chosen to sit below the gateway
  floor and make the argument visible.
- That the **operator** is the right party to hold consent, rather than the end user directly.
- That agent traffic volumes would make the aggregation economics work at all.
