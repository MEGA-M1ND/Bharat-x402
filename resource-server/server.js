/**
 * Bharat x402 — resource server.
 *
 * Plays the Indian publisher: serves a premium market report, but only to
 * agents that have paid ₹5 for it. The payment gate is the real @x402/express
 * middleware; the only thing swapped out is the scheme and the facilitator it
 * talks to. See x402-config.js for how that substitution works.
 *
 * Request lifecycle for a protected route:
 *
 *   1. No X-PAYMENT header      → 402 + `accepts[]` describing the INR offer
 *   2. X-PAYMENT present        → middleware POSTs it to facilitator /verify
 *   3. verify ok                → our handler runs, but its response is buffered
 *   4. handler returns 2xx      → middleware POSTs to facilitator /settle
 *   5. settle ok                → buffered body is released, plus X-PAYMENT-RESPONSE
 *
 * Step 4-5 is why deferred settlement fits so cleanly: `/settle` books a
 * commitment against the ledger and returns immediately, while the actual
 * rupees move later in one batched Razorpay Payment Link.
 */

"use strict";

require("dotenv").config();

const express = require("express");
const { paymentMiddleware, x402ResourceServer } = require("@x402/express");
const { HTTPFacilitatorClient } = require("@x402/core/server");

const {
  SCHEME,
  NETWORK,
  RazorpayInrScheme,
  PREMIUM_MARKET_REPORT,
  buildRoutes,
} = require("./x402-config");

const PORT = Number(process.env.PORT || 3402);
const FACILITATOR_URL = (process.env.FACILITATOR_URL || "http://localhost:8402").replace(/\/+$/, "");
const PAY_TO = process.env.PAY_TO || "acc_BharatNewsNetwork";
const PRICE = process.env.RESOURCE_PRICE || "₹5.00";

const app = express();
app.use(express.json());

/**
 * Header compatibility shim.
 *
 * The x402 payment proof header has two names in the wild. `X-PAYMENT` is the
 * original, and it is what most write-ups (and Cloudflare's own docs) describe.
 * @x402/core 2.x renamed it to `PAYMENT-SIGNATURE`.
 *
 * The library is not internally consistent about this: its *client* still sends
 * `X-PAYMENT` (`client/index.js` → `{"X-PAYMENT": encodePaymentSignatureHeader(...)}`),
 * but its *server* reads only `PAYMENT-SIGNATURE`
 * (`x402HTTPResourceServer.extractPayment`). A payload sent under `X-PAYMENT`
 * is therefore silently treated as no payment at all — the request comes back
 * as a plain 402 and the facilitator never sees it.
 *
 * Rather than pick a side, we accept either and normalise to what the
 * middleware reads. Costs three lines and means an agent written against
 * either generation of the docs works against this server.
 */
app.use((req, res, next) => {
  if (!req.headers["payment-signature"] && req.headers["x-payment"]) {
    req.headers["payment-signature"] = req.headers["x-payment"];
  }

  // Same split in the response direction: the settlement receipt goes out as
  // `PAYMENT-RESPONSE` in v2 and `X-PAYMENT-RESPONSE` in v1. Mirror it so a
  // client of either generation can read its receipt. Wrapping setHeader is
  // the least invasive way to catch a header the middleware sets later.
  const originalSetHeader = res.setHeader.bind(res);
  res.setHeader = function (name, value) {
    if (String(name).toLowerCase() === "payment-response") {
      originalSetHeader("X-PAYMENT-RESPONSE", value);
    }
    return originalSetHeader(name, value);
  };

  next();
});

/**
 * Minimal structured logging. Every service in this project emits one JSON
 * object per event so the whole demo can be replayed from logs — the audit
 * trail matters more here than pretty output.
 *
 * @param {string} event Event name.
 * @param {object} [fields] Extra structured fields.
 */
function log(event, fields = {}) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), service: "resource-server", event, ...fields }));
}

// ---------------------------------------------------------------------------
// x402 wiring
// ---------------------------------------------------------------------------

/**
 * The facilitator client. `HTTPFacilitatorClient` is stock x402 — it speaks the
 * standard facilitator contract (`POST /verify`, `POST /settle`, `GET /supported`).
 *
 * In the reference deployment this URL is https://x402.org/facilitator, which
 * settles USDC on Base. Ours is a FastAPI service that settles rupees through
 * Razorpay. The library does not know or care about the difference — which is
 * exactly the claim this project is making.
 */
const facilitatorClient = new HTTPFacilitatorClient({
  url: FACILITATOR_URL,
  timeoutMs: 10_000,
});

/**
 * Register our INR scheme against the resource server. `register(network, scheme)`
 * is the documented extension point; nothing here is monkey-patched.
 */
const resourceServer = new x402ResourceServer(facilitatorClient).register(
  NETWORK,
  new RazorpayInrScheme({ facilitatorUrl: FACILITATOR_URL })
);

/**
 * Audit hooks. Nothing about a payment decision should be invisible to the
 * publisher — a rejected payment logs *why* it was rejected, on the publisher's
 * side, independent of whatever the facilitator wrote to its own ledger.
 */
resourceServer.onVerifyFailure(async ({ requirements, error }) => {
  log("payment_verify_failed", {
    scheme: requirements.scheme,
    network: requirements.network,
    amount: requirements.amount,
    reason: error.message,
  });
});

resourceServer.onAfterVerify(async ({ requirements }) => {
  log("payment_verified", {
    scheme: requirements.scheme,
    network: requirements.network,
    amount: requirements.amount,
  });
});

resourceServer.onSettleFailure(async ({ requirements, error }) => {
  log("payment_settle_failed", { amount: requirements.amount, reason: error.message });
});

const routes = buildRoutes({ payTo: PAY_TO, price: PRICE, facilitatorUrl: FACILITATOR_URL });

/**
 * Mount the gate. The middleware calls `GET /supported` on the facilitator at
 * startup to confirm it actually handles razorpay-inr on razorpay:inr-test.
 *
 * This is mounted unconditionally and before the protected handler. If the
 * facilitator is unreachable the middleware fails the request — it never falls
 * through to serving paid content for free, which is the one failure mode a
 * paywall must not have.
 */
app.use(paymentMiddleware(routes, resourceServer));

/**
 * Waits for the facilitator to answer `GET /supported`.
 *
 * Without this the very first protected request loses a race with the
 * middleware's own startup fetch and 500s, which makes for a poor first
 * impression when someone follows the demo script. Polling until the
 * facilitator is up means the port opens only when the gate genuinely works.
 *
 * @param {number} [attempts] How many times to try.
 * @param {number} [delayMs] Gap between attempts.
 * @returns {Promise<boolean>} Whether the facilitator answered in time.
 */
async function waitForFacilitator(attempts = 15, delayMs = 1000) {
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const supported = await facilitatorClient.getSupported();
      const kinds = supported.kinds.map((k) => `${k.scheme}@${k.network}`);
      log("facilitator_ready", { attempt, kinds });
      return true;
    } catch (err) {
      if (attempt === attempts) {
        log("facilitator_unreachable", { attempts, url: FACILITATOR_URL, message: err.message });
        return false;
      }
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/**
 * The gated resource. By the time Express reaches this handler the middleware
 * has already verified payment — so the handler itself stays free of any
 * payment logic, which is the ergonomic win x402 is after.
 */
app.get("/premium/market-report", (req, res) => {
  const payer = req.header("x-payment") ? "paid-agent" : "unknown";
  log("premium_resource_served", { resourceId: PREMIUM_MARKET_REPORT.resourceId, payer });

  res.json({
    ...PREMIUM_MARKET_REPORT,
    servedAt: new Date().toISOString(),
  });
});

/** Free sample, so an agent can decide whether the paid fetch is worth ₹5. */
app.get("/free/market-report-preview", (_req, res) => {
  res.json({
    title: PREMIUM_MARKET_REPORT.title,
    publisher: PREMIUM_MARKET_REPORT.publisher,
    summary: PREMIUM_MARKET_REPORT.summary,
    paidVersion: "/premium/market-report",
    price: PRICE,
  });
});

app.get("/health", (_req, res) => {
  res.json({ service: "resource-server", status: "ok", facilitator: FACILITATOR_URL });
});

app.get("/", (_req, res) => {
  res.json({
    service: "Bharat x402 resource server",
    description: "Publisher content gated behind x402, priced in INR, settled via Razorpay.",
    scheme: SCHEME,
    network: NETWORK,
    price: PRICE,
    facilitator: FACILITATOR_URL,
    routes: {
      free: "/free/market-report-preview",
      paid: "/premium/market-report",
      health: "/health",
    },
  });
});

/**
 * Catch-all error handler. Nothing fails silently: a payment path that blows up
 * gets logged with its stack before the client sees a 500.
 */
app.use((err, _req, res, _next) => {
  log("unhandled_error", { message: err.message, stack: err.stack });
  res.status(500).json({ error: "internal_error", message: err.message });
});

/**
 * Boots the server once the facilitator is answering.
 *
 * @returns {Promise<void>}
 */
async function start() {
  const ready = await waitForFacilitator();
  if (!ready) {
    // Start anyway: better to serve 502s on the paid route and keep the free
    // routes up than to take the whole publisher offline because settlement
    // is down. The gate stays mounted either way.
    console.warn(`[resource-server] facilitator at ${FACILITATOR_URL} did not respond — paid routes will fail until it does`);
  }

  app.listen(PORT, () => {
    log("server_started", {
      port: PORT,
      facilitator: FACILITATOR_URL,
      facilitatorReady: ready,
      scheme: SCHEME,
      network: NETWORK,
      price: PRICE,
      payTo: PAY_TO,
    });
    console.log(`[resource-server] http://localhost:${PORT} — paid route: /premium/market-report (${PRICE})`);
  });
}

if (require.main === module) {
  start();
}

module.exports = { app, start, waitForFacilitator };
