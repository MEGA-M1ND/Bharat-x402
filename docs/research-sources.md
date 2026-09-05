# Primary sources

Every external claim this repository makes is listed here with the official source it came from,
and — where the source does not fully support the claim — the narrower claim actually made instead.
Secondary summaries (blogs, news, aggregators) are not used where a primary source exists.

Checked **September 2026**. Payment platform behaviour changes; re-verify before relying on any of
it.

---

## x402 protocol

| Claim | Source |
| --- | --- |
| **x402 was authored by Coinbase**, not Cloudflare | [Cloudflare blog, *Launching the x402 Foundation with Coinbase*](https://blog.cloudflare.com/x402/) — "Coinbase authored the x402 transaction flow […] to help machines pay directly for resources over HTTP." |
| Cloudflare is a **co-founder of the x402 Foundation**, not the protocol's author | Same source — "Cloudflare is partnering with Coinbase to create the x402 Foundation." |
| The protocol is version **2**, and `x402Version: 2` is required | [x402 specification v2](https://github.com/coinbase/x402/blob/main/specs/x402-specification-v2.md) §5.1.2 |
| `accepts[]` entries carry `scheme`, `network`, `amount`, `asset`, `payTo`, `maxTimeoutSeconds`, `extra` | Spec v2 §5.1.2 |
| **`asset` may be an ISO 4217 currency code for fiat** | Spec v2 §5.1.2 — "Token contract address **or ISO 4217 currency code for fiat**" |
| **`payTo` may be a role constant**, not only a wallet address | Spec v2 §5.1.2 — "Recipient wallet address **or role constant (e.g., 'merchant')**" |
| **Non-blockchain networks are expected to use CAIP-2 form** — so `razorpay:inr-test` is spec-shaped | Spec v2 §11.1 — "Non-blockchain networks are encouraged to follow the CAIP-2 format (e.g., `ach:us`, `sepa:eu`)." |
| A **`deferred` scheme is contemplated by the spec's own architecture**, alongside `exact` | Spec v2 §Architecture — "payment scheme (e.g., exact, deferred)" |
| `/supported` returns `kinds`, `extensions`, `signers`; `extensions` is "Array of extension identifiers the facilitator has implemented" | Spec v2 §7.3, §7.3.1 |
| Extensions are advertised in `PaymentRequired` and echoed by the client | Spec v2 §5.1.2 |
| `VerifyResponse` is `{isValid, invalidReason?, payer?}`; `SettleResponse` is `{success, errorReason?, payer?, transaction, network, amount?, extensions?}` | Spec v2 §5.3.2, §5.4.2 |

**Explicitly not claimed.** The spec names `deferred` as an example scheme but does **not** define
its semantics. `razorpay-inr` is therefore this project's own scheme, and its deferred behaviour is
documented in [protocol-extension.md](protocol-extension.md) rather than presented as standard.

## Cloudflare Pay Per Crawl / AI Crawl Control

| Claim | Source |
| --- | --- |
| Pay Per Crawl lets site owners charge AI crawlers per access | [Cloudflare AI Crawl Control docs — Pay Per Crawl](https://developers.cloudflare.com/ai-crawl-control/features/pay-per-crawl/) |
| It uses **its own `crawler-*` headers**, not x402 — `crawler-price`, `crawler-exact-price`, `crawler-max-price`, `crawler-charged`, `crawler-error` | [Crawl pages](https://developers.cloudflare.com/ai-crawl-control/features/pay-per-crawl/use-pay-per-crawl-as-ai-owner/crawl-pages/) |
| Crawler identity uses **Web Bot Auth**, with the payment headers covered by the request signature | Same source — payment headers must be "included in the `signature-input` header components" |
| Cloudflare's **Monetization Gateway** is the x402-based generalisation of Pay Per Crawl | [Cloudflare blog, *Announcing the Monetization Gateway*](https://blog.cloudflare.com/monetization-gateway/) |
| In the Monetization Gateway, **content is released after payment settles** | Same source |

**Consequence for this repository.** Pay Per Crawl and x402 are *two different mechanisms*.
Pay Per Crawl motivates the use case; it is not the protocol implemented here, and Cloudflare is
not a runtime dependency of this project.

## Razorpay

| Claim | Source |
| --- | --- |
| **Payment Links reject INR amounts below 100 subunits (₹1.00)** | [Create a Standard Payment Link](https://razorpay.com/docs/api/payments/payment-links/create-standard/) — "amount should be minimum 100 for INR", amount is "in the smallest unit of the currency" |
| `reference_id` must be unique per Payment Link, max 40 characters | Same source |
| Webhook signatures are **HMAC-SHA256 over the raw request body**, in `X-Razorpay-Signature` | [Validate and Test Webhooks](https://razorpay.com/docs/webhooks/validate-test/) — "with your webhook secret set as the key and the webhook request body as the message" |
| The **raw** body must be used — "Do not parse or cast the webhook request body" | Same source |
| **`x-razorpay-event-id` is unique per event** and is the documented deduplication key | Same source |
| Webhooks may **arrive out of order and be duplicated** | Same source |
| Failed deliveries retry with exponential backoff **for 24 hours**, after which the webhook is disabled | [Webhooks FAQs](https://razorpay.com/docs/webhooks/faqs/) |
| **UPI Reserve Pay is gated**: "Raise a request with our Support team to get UPI Reserve Pay activated on your account" | [UPI Reserve Pay](https://razorpay.com/docs/payments/recurring-payments/upi-reserve-pay/) |
| Reserve Pay block ceiling **₹10,000**, validity **up to 90 days**, multiple debits until exhausted or expired | Same source |
| Razorpay + NPCI announced **Agentic Payments for Claude on 20 February 2026**, in **pilot** with selected users, built on UPI Reserve Pay | [Razorpay blog, *Razorpay & NPCI: Agentic Payments for UPI on Claude*](https://razorpay.com/blog/agentic-payments-and-npci/) |
| Consent model: "a one-time, consent-based authorization by setting spending limits for a merchant", revocable instantly | Same source |

### Observed behaviour versus documented guarantee

Two things this repository reports are **observations from the test API**, not platform-wide
guarantees, and are labelled as such wherever they appear:

1. **The rejection string.** Posting 50 paise to `POST /v1/payment_links` on a `rzp_test_` key
   returned `amount: amount should be minimum 1.00 for INR.` The official documentation words the
   same rule as "minimum 100 for INR" (subunits). Both describe a ₹1 floor **for Payment Links**;
   the rupee-formatted string is what the API actually returned to us, and is quoted as such.
2. **`429 Too many requests`** on back-to-back Payment Link creation. Observed; Razorpay does not
   publish a rate limit for this endpoint, so no specific threshold is claimed.

Neither observation is generalised to other Razorpay products, to UPI as a whole, or to NPCI.

## NPCI

| Claim | Source |
| --- | --- |
| UPI Single Block Multiple Debits (SBMD) is an NPCI mandate feature | [NPCI circular UPI/OC-200/FY-24-25 — *Enablement of UPI Mandate feature of Single Block Multiple Debits*](https://www.npci.org.in/PDF/npci/upi/circular/2024/UPI-OC-No-200-FY-24-25%E2%80%93Enablement-of-UPI-Mandate-feature-of-Single-Block-Multiple-Debits.pdf) |
| SBMD was renamed **UPI Reserve Pay**; funds are blocked once and debited repeatedly until the block is exhausted or the mandate expires | Razorpay's UPI Reserve Pay documentation (above) |

**Not claimed.** This project has no NPCI integration, no UPI mandate, and no bank relationship.
`facilitator/reserve_pay.py` is a **domain simulation** that models the documented block/debit
shape and its published ₹10,000 / 90-day limits. It refuses to run outside `MOCK_RAZORPAY`.

---

## Unverified assumptions

Stated plainly, because a reviewer should not have to go looking for them:

- **No public UPI Reserve Pay API surface was available** to this project. Activation is gated
  behind a Razorpay support request and an eligibility review, so the request and response shapes
  in `reserve_pay.py` are invented to be *plausible*, not reproduced from a specification.
- **Razorpay settlement — the payout into a merchant's bank account — is not modelled against a
  live account.** The publisher-payable side of the ledger is internal book-keeping only.
- **Gateway fee percentages** in `razorpay_client.py`'s cost model are configuration, not quoted
  pricing. They exist to make the arithmetic checkable, not to state what anyone is charged.
- **The x402 specification is a moving target.** Version 2 is current as of September 2026 and the
  `deferred` scheme is named in it but left undefined, so this project's deferred semantics could
  diverge from a future standard one.
