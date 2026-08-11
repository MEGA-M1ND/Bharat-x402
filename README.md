# Bharat x402

An **INR settlement facilitator for Cloudflare's x402 agent-payment protocol** — so Indian
publishers and API providers can charge AI agents per request in rupees, through Razorpay,
without touching stablecoin infrastructure.

> Phase 0 skeleton. This README is rewritten in Phase 6 with a quickstart, architecture
> diagram, and measured results from a real demo run.

## Repository layout

| Path | What it is |
| --- | --- |
| `resource-server/` | Express app playing the publisher — gates `/premium/market-report` behind HTTP 402 |
| `facilitator/` | **The new piece.** FastAPI service that verifies x402 payment payloads and settles them via Razorpay instead of an EVM chain |
| `demo-agent/` | Python script simulating an AI crawler paying its way past the paywall |
| `reporting/` | Reads the ledger and prints a publisher-facing revenue summary |
| `docs/` | Architecture notes and a step-by-step demo script |
| `tests/` | End-to-end flow tests |

## Status

- [x] Phase 0 — repo skeleton, env templates, docker-compose
- [x] Phase 1 — resource server with x402 gate
- [ ] Phase 2 — Razorpay facilitator (`/verify`, `/settle`, `/settle-batch`)
- [ ] Phase 3 — demo crawler agent
- [ ] Phase 4 — batch settlement + daily summary
- [ ] Phase 5 — tests
- [ ] Phase 6 — documentation

## This is a real x402 deployment, not a lookalike

The resource server runs the stock `@x402/express` middleware. x402 separates the
*scheme* (how a payment is proven), the *network* (where it settles), and the
*facilitator* (who settles it) — and all three are open extension points:
`Network` is typed `` `${string}:${string}` ``, and `FacilitatorClient` /
`SchemeNetworkServer` are plain interfaces.

So this project registers a `razorpay-inr` scheme on network `razorpay:inr-test`
and points the middleware's facilitator client at a FastAPI service that speaks the
standard facilitator contract (`GET /supported`, `POST /verify`, `POST /settle`).
No part of the protocol is reimplemented or stubbed.

An unpaid request today:

```
HTTP 402 Payment Required
PAYMENT-REQUIRED: <base64>   ← {"x402Version":2, "accepts":[{"scheme":"razorpay-inr",
                                 "network":"razorpay:inr-test","asset":"INR",
                                 "amount":"500", ...}]}
```

`amount` is in **paise**, exactly as USDC amounts travel in atomic 1e-6 units.
₹5.00 → `"500"`. No float ever touches a monetary value.

## Test mode only

This is a portfolio demo, not a payment system. It uses **Razorpay test-mode keys
exclusively** and refuses to start if handed a `rzp_live_` key. It can also run fully
offline with `MOCK_RAZORPAY=true`.
