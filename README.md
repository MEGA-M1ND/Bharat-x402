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
- [ ] Phase 1 — resource server with x402 gate
- [ ] Phase 2 — Razorpay facilitator (`/verify`, `/settle`, `/settle-batch`)
- [ ] Phase 3 — demo crawler agent
- [ ] Phase 4 — batch settlement + daily summary
- [ ] Phase 5 — tests
- [ ] Phase 6 — documentation

## Test mode only

This is a portfolio demo, not a payment system. It uses **Razorpay test-mode keys
exclusively** and refuses to start if handed a `rzp_live_` key. It can also run fully
offline with `MOCK_RAZORPAY=true`.
