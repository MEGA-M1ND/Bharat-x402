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
[resource-server] http://localhost:3402 — console + paid routes (₹5.00 / ₹0.50)
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

{"error":"payment_required","message":"Paid resource. ₹5.00 per fetch, settled in INR via
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
    "extra": { "humanAmount": "₹5.00", "settlementMode": "deferred", "proofScheme": "ed25519" }
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

  key   agent-perplexity-bot
        public  H4wYhPCvPa2eJHzBdA7bfGYe…  (registered now)
        private key never leaves this process — the facilitator
        holds only the public half and cannot forge a commitment.

[1/5] Request the resource with no payment attached
      -> HTTP 402 Payment Required

      accepts[0], decoded from the PAYMENT-REQUIRED header:
        scheme    razorpay-inr          not 'exact' — this is not an EVM transfer
        network   razorpay:inr-test     not a blockchain, just a settlement rail
        amount    500 paise             = ₹5.00
        payTo     acc_BharatNewsNetwork the publisher's merchant account
        settles   deferred / razorpay   batched, not per-request

[2/5] Ask the facilitator to quote this fetch
      -> off_482222ab8a1c42f2829f  (₹5.00)
        issued 2026-08-24T05:13:18Z, expires 2026-08-24T05:18:18Z
        single-use, and bound to this agent id

[3/5] Sign acceptance of the quote
      Ed25519 over the canonical JSON of:
        {"acceptedAt":"2026-08-24T05:13:18Z","agentId":"agent-perplexity-bot",…
      -> uVe+CT7JsO1S37fSZ/nytPiOO05b4x3L…  (88 chars)
      Signed with this agent's own private key. The facilitator
      can verify this and cannot produce it — so the commitment
      is evidence in a dispute, not just a checksum.

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
live `rzp.io` link you can open:

```json
{"event": "settlement_run", "batches": 5, "created": 5, "totalPaise": 3500,
 "paymentLinks": ["plink_TOk88dBGlGTOeq", "plink_TOk89ZNUFJR7oG", ...]}
```

If the credentials are wrong, you find out at **startup** rather than at settlement:

```json
{"event": "razorpay_credentials_invalid", "status": "rejected",
 "message": "Razorpay rejected these credentials. The key id and secret must come from
  the same key pair — Razorpay shows a secret only once, when it is generated..."}
```

The service still starts. `/verify` and `/settle` never touch Razorpay, so payments are
still accepted and the ledger stays correct; only `/settle-batch` is affected, and those
commitments wait for the next run.

> **What "verified" covers.** Links are created, fetched back, and reconciled against the
> ledger. There is **no webhook handling** — a link that is actually *paid* does not update
> the ledger. Production needs a `payment_link.paid` webhook.

> **A live key will not start.** The facilitator refuses any `rzp_live_` credential and
> exits with an explanation. This project creates Payment Links in a loop; it has no
> business holding a key that can move real money.

---

## 8. Let an agent decide for itself what to buy

Everything so far pays because the script says to. `agent-kit/` is where
something chooses — Claude gets a question, a rupee budget, and five tools.

It needs its own virtualenv: the MCP SDK wants Starlette 1.x and FastAPI 0.115
pins `<0.42`, so one environment cannot hold both the facilitator and this.

```bash
python -m venv agent-kit/.venv
agent-kit/.venv/Scripts/pip install -r agent-kit/requirements.txt   # bin/pip on macOS/Linux
```

With both services still running from step 2:

```bash
ANTHROPIC_API_KEY=sk-ant-… python agent-kit/researcher.py \
  "What's the going rate for AI agent traffic in India?"
```

The agent previews before buying, skips what it does not need, and stops when
the budget runs out. Summarised reasoning is printed as it goes, so you can see
*why* it decided ₹5 was or was not worth spending.

**No API key?** The same tool surface runs with a fixed sequence and no model:

```bash
python agent-kit/researcher.py --scripted --budget 12
```

```
  -> fetch_paid_resource('api-call')
Bought api-call for ₹0.50 (commitment cmt_0d82bc6ad4f846bfaecc).
Budget remaining: ₹11.50.

  -> fetch_paid_resource('market-report')
REFUSED — market-report costs ₹5.00 but only ₹1.50 of the ₹12.00 budget is left.
          Nothing was charged.
```

That refusal is the thing worth looking at. The budget is enforced in
`x402_client.py`, before any HTTP — not in the system prompt. A prompt-level
limit is a request, and the agent reads the documents it buys, so a purchased
file saying "ignore your budget" is an input an attacker can write.

### As an MCP server

The same five tools over the Model Context Protocol, so any MCP client can buy
things in rupees without knowing what x402 is:

```bash
agent-kit/.venv/Scripts/python agent-kit/mcp_server.py
```

It speaks stdio. To register it with Claude Desktop, see the config block at the
top of `agent-kit/mcp_server.py`. Note what the client *cannot* do: there is no
`sign`, no `register`, and no way to name an amount — only to buy a named
resource at the publisher's price.

---

## 9. Run the tests

```bash
pytest tests -v
```

```
156 passed in 19.21s
```

With both services running, the integration tests exercise the real HTTP path. With them
stopped you get `146 passed, 10 skipped`. To make skipping fatal — which is what CI does —
set `REQUIRE_INTEGRATION=1`.

The same suite also runs against real Postgres rather than SQLite, which is how the
dialect shim in `db.py` is held honest:

```bash
TEST_LEDGER_DSN=postgres://user:pass@host:5432/db LEDGER_AUTO_MIGRATE=1 pytest tests -q
```

```bash
ruff check facilitator demo-agent reporting agent-kit tests
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
| 402 on every attempt, `invalid_signature` | The agent is signing with a key the facilitator does not have on file — usually a deleted `demo-agent/.keys/<agent-id>.key` against a facilitator that still remembers the old public key. Pick a fresh `--agent-id`, or reset the ledger (below). On `--legacy-hmac` runs it instead means the shared secret differs between `.env` files, *or* that this agent has a registered key and is correctly being refused the downgrade. |
| `agent-… is registered with a different public key` | Same cause, reported at registration instead of at payment. Rebinding an id to a new key is refused deliberately — see `Ledger.register_agent`. |
| `agent_not_registered` | The facilitator has `ALLOW_HMAC_FALLBACK=false`, so an agent must register an Ed25519 key before paying. Drop `--legacy-hmac`. |
| `webhooks_not_configured` on `/webhooks/razorpay` | No `RAZORPAY_WEBHOOK_SECRET` is set. The endpoint fails closed rather than accepting unauthenticated ledger writes. |
| `offer_expired` | Offers last 5 minutes. Raise `OFFER_TTL_SECONDS` if you are stepping through by hand. |
| `503 settlement_unavailable` | The publisher started before the facilitator and is still waiting. It fails closed rather than serving paid content free. |
| Garbled `₹` or box characters | A console that is not UTF-8. Both scripts fall back to ASCII, but `chcp 65001` on Windows fixes it properly. |
| `ModuleNotFoundError: No module named 'ledger'` | Run from the repo root, or use `--app-dir facilitator` as shown in step 2. |

## Resetting

```bash
rm facilitator/data/ledger.db
```

The schema is recreated on next start.
