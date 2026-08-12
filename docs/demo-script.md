# Demo script

Every command below was run against a clean clone. Expected output is quoted verbatim,
with only ids and timestamps differing between runs.

**Nothing here needs a Razorpay account.** The facilitator defaults to `MOCK_RAZORPAY=true`
and fabricates responses shaped like the real API. [Step 7](#7-optional-run-against-real-razorpay-test-keys)
covers switching to real test keys.

Total time from clone to a paid fetch: about three minutes, most of it `npm install`.

---

## 0. Prerequisites

- **Node 20+** and **Python 3.12+**
- No database to install — the ledger is SQLite, created on first run.

---

## 1. Set up

```bash
git clone https://github.com/MEGA-M1ND/Bharat-x402.git
cd Bharat-x402
```

```bash
python -m venv .venv
```

Activate it — `source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\Activate.ps1`
on Windows PowerShell.

```bash
pip install -r facilitator/requirements.txt -r demo-agent/requirements.txt -r requirements-dev.txt
```

```bash
npm --prefix resource-server install
```

Create the three `.env` files from their templates. The defaults work as-is:

```bash
cp facilitator/.env.example facilitator/.env
cp resource-server/.env.example resource-server/.env
cp demo-agent/.env.example demo-agent/.env
```

On Windows PowerShell, use `Copy-Item` instead of `cp`.

---

## 2. Start the two services

Each needs its own terminal, both with the virtualenv active.

**Terminal 1 — the INR facilitator** (port 8402):

```bash
python -m uvicorn main:app --port 8402 --app-dir facilitator
```

Expected:

```
{"event": "facilitator_started", "network": "razorpay:inr-test", "razorpayMode": "mock",
 "scheme": "razorpay-inr", "service": "facilitator", "settlementMode": "deferred", "status": "ok", ...}
INFO:     Uvicorn running on http://127.0.0.1:8402
```

`"razorpayMode": "mock"` confirms no real API calls will be made.

**Terminal 2 — the publisher** (port 3402):

```bash
node resource-server/server.js
```

Expected:

```
{"ts":"...","service":"resource-server","event":"facilitator_ready","attempt":1,
 "kinds":["razorpay-inr@razorpay:inr-test"]}
[resource-server] http://localhost:3402 — paid route: /premium/market-report (5.00)
```

`facilitator_ready` means the stock x402 middleware called `GET /supported`, was told this
facilitator settles `razorpay-inr` on `razorpay:inr-test`, and accepted it. That handshake
is what makes the rest work.

> The server waits for the facilitator before opening its port, so start order does not
> matter. It will retry for 15 seconds.

---

## 3. See the paywall

```bash
curl -i http://localhost:3402/premium/market-report
```

```
HTTP/1.1 402 Payment Required
PAYMENT-REQUIRED: eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQYXltZW50IHJlcXVpcmVkIiwi...
Content-Type: application/json; charset=utf-8

{"error":"payment_required","message":"Paid resource. 5.00 per fetch, settled in INR via
Razorpay...","offer":{"resourceId":"market-report-2026-08","scheme":"razorpay-inr",
"network":"razorpay:inr-test","asset":"INR","amount":"500","humanAmount":"₹5.00",...}}
```

The `PAYMENT-REQUIRED` header is the machine-readable offer and is what a conforming
client reads. Decode it:

```bash
curl -sD - -o /dev/null http://localhost:3402/premium/market-report | grep -i '^payment-required:' | cut -d' ' -f2- | base64 -d
```

```json
{
  "x402Version": 2,
  "resource": { "serviceName": "Bharat News Network" },
  "accepts": [{
    "scheme": "razorpay-inr",
    "network": "razorpay:inr-test",
    "amount": "500",
    "asset": "INR",
    "payTo": "acc_BharatNewsNetwork",
    "maxTimeoutSeconds": 300,
    "extra": { "humanAmount": "₹5.00", "settlementMode": "deferred", "proofScheme": "hmac-sha256" }
  }]
}
```

`amount` is **paise**, exactly as USDC amounts travel in atomic units. ₹5.00 → `"500"`.

---

## 4. Watch an agent pay

```bash
python demo-agent/crawler_agent.py
```

```
══════════════════════════════════════════════════════════════════════════
  Bharat x402 | AI crawler agent
  agent-perplexity-bot -> http://localhost:3402/premium/market-report
══════════════════════════════════════════════════════════════════════════

[1/5] Request the resource with no payment attached
      -> HTTP 402 Payment Required

      accepts[0], decoded from the PAYMENT-REQUIRED header:
        scheme    razorpay-inr          not 'exact' — this is not an EVM transfer
        network   razorpay:inr-test     not a blockchain, just a settlement rail
        amount    500 paise             = ₹5.00
        payTo     acc_BharatNewsNetwork the publisher's merchant account
        settles   deferred / razorpay   batched, not per-request

[2/5] Ask the facilitator to quote this fetch
      -> off_9e76db038c1d49a29b85  (₹5.00)
        single-use, and bound to this agent id

[3/5] Sign acceptance of the quote
      -> bbc46770994d770435254cd576b52cd6…  (64 hex chars)

[4/5] Retry the request with the payment attached
      -> HTTP 200 OK

[5/5] Read the settlement receipt
        success       True
        transaction   cmt_675a947e702b466190e3
        mode          deferred
        Commitment recorded; rupees move in the next batched Payment Link.

──────────────────────────────────────────────────────────────────────────
  CONTENT UNLOCKED
──────────────────────────────────────────────────────────────────────────
  India Digital Payments Market Report — August 2026
  ...

──────────────────────────────────────────────────────────────────────────
  Paid ₹5.00 in 4 HTTP requests.
  No rupees have moved yet. This fetch is one line in a batch
  that becomes a single Razorpay Payment Link at end of day —
  which is the only way a charge this small is collectable at all.
──────────────────────────────────────────────────────────────────────────
```

That is the whole protocol. `transaction` is a commitment id, not a chain hash — an honest
identifier for what actually happened: a debt was recorded.

---

## 5. Simulate a day, then settle it

Generate traffic from several crawler identities:

```bash
python demo-agent/crawler_agent.py --count 9
```

```
    1. agent-gptbot                ₹5.00  paid  cmt_d7e452187c2a4bbd9e02
    2. agent-claude-web            ₹5.00  paid  cmt_93def1938e3947dcbec8
    ...
  9/9 fetches paid, ₹45.00 committed.
```

Preview settlement without charging anything:

```bash
python facilitator/scheduler.py --once --dry-run
```

```json
{"batches": 4, "commitments": 9, "created": 0, "dryRun": true, "event": "settlement_run",
 "totalPaise": 4500, "service": "scheduler"}
```

Settle for real:

```bash
python facilitator/scheduler.py --once
```

```json
{"batches": 4, "commitments": 9, "created": 4, "dryRun": false, "event": "settlement_run",
 "paymentLinks": ["plink_MOCK9b0d796684bd56", "plink_MOCKe7ac87d3bdf11f", ...],
 "totalPaise": 4500, "service": "scheduler"}
```

Nine requests became **four** Payment Links — one per paying agent.

Run it again to confirm it is safe to over-schedule:

```bash
python facilitator/scheduler.py --once
```

```json
{"event": "settlement_noop", "note": "nothing pending", "service": "scheduler"}
```

---

## 6. Read the publisher's report

```bash
python reporting/daily_summary.py
```

```
┌────────────────────────────────────────────────────────┐
│ *Bharat News Network*                                  │
│ Daily agent revenue · 11 Aug 2026                      │
│                                                        │
│ 💰 *₹45.00* earned from AI crawlers                    │
│ 📊 9 paid requests · 4 agents                          │
│                                                        │
│ *Top payers*                                           │
│ 1. agent-claude-web — 5 req · ₹25.00                   │
│ 2. agent-bytespider — 2 req · ₹10.00                   │
│ ...                                                    │
│                                                        │
│ *Settlement*                                           │
│ ✅ 4 Payment Links · ₹45.00                            │
│ ```                                                    │
│ plink_MOCK9b0d796684bd56     ₹10.00    2 req           │
│ ...                                                    │
│ ```                                                    │
│ 🔗 https://rzp.io/i/mock-6684bd56                      │
└────────────────────────────────────────────────────────┘
```

Formatted as a WhatsApp message on purpose — a regional publisher will not log into a
dashboard to check whether crawlers paid them. `--json` gives the machine-readable form,
`--plain` drops the emoji.

### The scenario that makes the argument

At ₹5 a fetch the report says, correctly, that batching saves API calls rather than fees.
The interesting case is sub-rupee API pricing. Stop the resource server, then:

```bash
RESOURCE_PRICE=0.50 node resource-server/server.js
```

(PowerShell: `$env:RESOURCE_PRICE="0.50"; node resource-server/server.js`)

```bash
python demo-agent/crawler_agent.py --count 60 --quiet
python facilitator/scheduler.py --once
python reporting/daily_summary.py
```

```
│ *Why batching*                                         │
│ ⚡ 60 agent requests collected in 5 gateway charges.   │
│ 🚧 ₹30.00 of this could not have been collected at all │
│ per-request — 60 charges sit under Razorpay's ₹1.00    │
│ minimum.                                               │
```

**₹30.00 of ₹30.00 unreachable.** Every one of those 60 charges is below Razorpay's
minimum order value, so per-request settlement cannot collect that revenue at any fee.
Batching is not an optimisation here — it is the difference between the price point
existing and not.

---

## 7. Optional: run against real Razorpay test keys

Get test keys from the
[Razorpay dashboard](https://dashboard.razorpay.com/app/website-app-settings/api-keys)
(they start with `rzp_test_`), then in `facilitator/.env`:

```
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxx
MOCK_RAZORPAY=false
```

Restart the facilitator and confirm the mode changed:

```bash
curl -s http://localhost:8402/health
```

```json
{"service":"facilitator","status":"ok","settlementMode":"deferred","razorpayMode":"razorpay_test"}
```

Batches now create real Payment Links in your test dashboard, and `paymentLinkUrl` is a
live `rzp.io` link you can open.

> **A live key will not start.** The facilitator refuses any `rzp_live_` credential and
> exits with an explanation. This project creates Payment Links in a loop; it has no
> business holding a key that can move real money.

---

## 8. Run the tests

```bash
pytest tests -v
```

```
60 passed in 11.70s
```

With both services running, the four integration tests exercise the real HTTP path. With
them stopped you get `52 passed, 4 skipped`. To make skipping fatal — which is what CI
does — set `REQUIRE_INTEGRATION=1`.

```bash
ruff check facilitator demo-agent reporting tests
```

---

## Docker

`docker-compose.yml` builds and runs both services:

```bash
docker compose up --build
```

> **Not verified.** The compose file and both Dockerfiles are written but were never run —
> Docker was not installed on the machine this was built on. The native path above is the
> tested one. If compose fails, that is why.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `unreachable  [Errno 111] Connection refused` | A service is not running. Check both terminals from step 2. |
| 402 on every attempt, `invalid_signature` | The HMAC secret differs between `.env` files. `X402_HMAC_SECRET` in `resource-server/.env` and `demo-agent/.env` must equal `FACILITATOR_HMAC_SECRET` in `facilitator/.env`. |
| `offer_expired` | Offers last 5 minutes. Raise `OFFER_TTL_SECONDS` if you are stepping through by hand. |
| `503 settlement_unavailable` | The publisher started before the facilitator and is still waiting. It fails closed rather than serving paid content free. |
| Garbled `₹` or box characters | A console that is not UTF-8. Both scripts fall back to ASCII, but `chcp 65001` on Windows fixes it properly. |
| `ModuleNotFoundError: No module named 'ledger'` | Run from the repo root, or use `--app-dir facilitator` as shown in step 2. |

## Resetting

```bash
rm facilitator/data/ledger.db
```

The schema is recreated on next start.
