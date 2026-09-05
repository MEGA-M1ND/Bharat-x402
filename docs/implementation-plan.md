# Implementation plan — from protocol demo to authorization/accrual/collection layer

**Thesis being built toward.** Bharat x402 is an INR-oriented authorization, metering,
credit-control, aggregation, and reconciliation layer for machine-priced HTTP access. Agents
negotiate through x402, but content is released only against operator-backed authority — reserved
funds, a prefunded test balance, or an explicit credit limit. Low-value usage is accumulated and
collected through a pluggable Razorpay-compatible settlement instrument.

**Working rule for this plan.** Ten concerns that the current codebase partly conflates must end up
separately named, separately stored, and separately testable:

| # | Concern | Answers |
| --- | --- | --- |
| 1 | Price negotiation | What does this resource cost, right now, for this caller? |
| 2 | Agent identity | Which key is speaking? |
| 3 | Operator consent & payment authority | Is this agent allowed to spend someone's money? |
| 4 | Usage authorization | May *this* request proceed, under all live policy? |
| 5 | Content fulfillment | Was the protected resource actually delivered? |
| 6 | Receivable accrual | What is now owed, by whom, to whom? |
| 7 | Aggregate collection | Which receivables are being charged together? |
| 8 | Gateway confirmation | What did the payment provider say happened? |
| 9 | Reconciliation | Do our records and theirs agree? |
| 10 | Publisher payable / payout | What do we owe the publisher, and did we pay it? |

---

## Baseline (recorded before any edit)

Commit `5b8e9e8`, branch `main`, working tree clean.

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **207 passed, 10 skipped** (integration tests skip without live services) |
| `ruff check facilitator demo-agent agent-kit reporting tests` | **All checks passed** |
| `node --check resource-server/server.js` | OK |
| `node --check resource-server/x402-config.js` | OK |

Environment note: the repository's `.venv` is the only environment with `psycopg`, `cryptography
46`, and `razorpay` installed. The machine-wide interpreter fails collection with
`ModuleNotFoundError: psycopg`. All checks in this plan are run with `./.venv/Scripts/python.exe`.

---

## Where this landed

Phases 1–4 are complete and committed. Phases 5–9 are not started, and the
repository is left at a clean boundary: the money lifecycle is representable end
to end (authority → reservation → fulfillment → receivable → collection →
confirmation), every money-changing operation posts a balanced journal
transaction, and no schema is half-migrated.

| Check | Baseline | Now |
| --- | --- | --- |
| `pytest -q` | 207 passed, 10 skipped | **362 passed** |
| `ruff check` | clean | clean |
| `node --check` | clean | clean |
| Migrations | 003 | **006** |
| Publicly readable operational endpoints | 4 | **0** |

## Phase status

- [x] **Phase 0** — Survey, primary-source research, gap analysis, baseline
- [x] **Phase 1** — Correct the domain model and public claims
- [x] **Phase 2** — Authenticated operators, agents, and consent
- [x] **Phase 3** — Back commitments with reserved authority
- [x] **Phase 4** — Double-entry ledger and complete money lifecycle
- [ ] **Phase 5** — Collection failure, refunds, and reconciliation
- [ ] **Phase 6** — x402 interoperability and multi-rail negotiation
- [ ] **Phase 7** — Optional Cloudflare publisher adapter
- [ ] **Phase 8** — Observability, security, and delivery quality
- [ ] **Phase 9** — Portfolio presentation

Phases 1–4 form a clean boundary: after Phase 4 the money lifecycle is representable end to end
(authority → reservation → fulfillment → receivable → collection → confirmation), every
money-changing operation posts a balanced journal transaction, and no partially-migrated schema is
left behind. Phases 5–9 are additive on top of that boundary and are recorded here as remaining.

---

## Phase 1 — Correct the domain model and public claims

Documentation and naming only; no behaviour change. Establishes the vocabulary the later phases
implement.

1. **Provenance.** x402 was authored by Coinbase, not Cloudflare. Correct every tracked file.
   Cloudflare is a co-founder of the x402 Foundation *with* Coinbase and a motivating use case
   (Pay Per Crawl), never a runtime dependency.
2. **Payment terminology.** Adopt the glossary below; purge "paid"/"earned"/"revenue" wherever only
   a commitment exists — including the dashboard's headline tile.
3. **Scope the Ed25519 claim** to "tamper-evident, non-facilitator-forgeable evidence that the
   registered agent key accepted a particular quote". Not proof of funds, not legal identity.
4. **Scope the ₹1 claim** to the Razorpay Payment Links API. The official docs *do* support ₹1 for
   this product; they do not support generalising it to every Razorpay product or to UPI at large.
5. **New docs**: `domain-model.md`, `threat-model.md`, `protocol-extension.md`, four ADRs, and
   Mermaid state diagrams for the success, collection-failure, refund, key-revocation, and
   lost-webhook paths.
6. **`/supported`** advertises the deferred semantics as a machine-discoverable x402 extension
   rather than leaving a client to infer them.
7. **Consistency test** (`tests/test_terminology.py`) that fails the build on regressions:
   "Cloudflare's x402", "earned", and commitment-as-payment phrasing.

## Phase 2 — Authenticated operators, agents, and consent

Migration `004`. New entities: `operators`, `merchants`, `agents` (extended), `agent_credentials`,
`spending_consents`, `api_credentials`.

- Scoped API keys, stored **only as hashes**, with rotation and revocation.
- Challenge–response agent key enrollment, replacing unauthenticated trust-on-first-use.
- Consent objects carrying per-request / daily / total limits in integer paise, publisher scope,
  and a validity window.
- Four planes: public protocol, agent-authorized, merchant/operator control, internal settlement.
  `agentId` as a query parameter stops being treated as authorization.
- `DEMO_UNSAFE_TOFU` preserves the old behaviour for the public demo; the production-like default
  is closed.

## Phase 3 — Back commitments with reserved authority

Migration `005`. `authority_accounts` and `reservations`.

- Reserve atomically **before** the handler runs; capture exactly once **after** fulfillment;
  release on failure.
- Backing types: `prefunded`, `simulated_reserve`, `credit`. Credit-backed usage is never called
  "paid".
- Publisher acceptance policy can require prefunded authority or a verified operator.

## Phase 4 — Double-entry ledger

Migration `006`. `accounts`, `journal_transactions`, `journal_entries`.

- Every money-changing operation posts a balanced transaction; nothing is ever mutated or deleted,
  only compensated.
- Invariant tests: debits equal credits, replay creates no second movement, capture never exceeds
  reservation, collection never exceeds outstanding, refund never exceeds collected, and every
  displayed aggregate recomputes from immutable records.

---

## Glossary (authoritative for all code and docs from Phase 1 onward)

| Term | Means | Does **not** mean |
| --- | --- | --- |
| **Quote** | A priced offer for a resource, signed and time-limited | A payment |
| **Acceptance** | The agent key's signed acceptance of a quote | That funds exist |
| **Authorization** | Approval to incur expense under operator consent and platform policy | That money moved |
| **Reservation** | An amount held against available authority before fulfillment | A debit |
| **Fulfillment** | Delivery of the protected content | Payment |
| **Commitment / receivable** | Amount owed after fulfillment | Cash |
| **Collection** | External movement of money from payer or operator | Confirmed receipt |
| **Gateway confirmation** | Signed or reconciled evidence of external payment state | Settlement into the bank |
| **Publisher payable** | Collected value owed to a publisher | Money already sent |
| **Payout** | Transfer of payable value to the publisher | — |
| **Reconciliation** | Comparison of internal records against gateway records | — |
