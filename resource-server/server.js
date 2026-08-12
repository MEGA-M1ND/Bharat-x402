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

// Load this service's own .env, not whatever happens to sit in the working
// directory. `node resource-server/server.js` from the repo root is the
// obvious way to start it, and dotenv's default would silently find nothing.
require("dotenv").config({ path: require("path").join(__dirname, ".env") });

const express = require("express");
const { paymentMiddleware, x402ResourceServer } = require("@x402/express");
const { HTTPFacilitatorClient } = require("@x402/core/server");

const {
  SCHEME,
  NETWORK,
  RazorpayInrScheme,
  PREMIUM_MARKET_REPORT,
  API_CALL_SAMPLE,
  buildRoutes,
} = require("./x402-config");

const PORT = Number(process.env.PORT || 3402);
const FACILITATOR_URL = (process.env.FACILITATOR_URL || "http://localhost:8402").replace(/\/+$/, "");
const PAY_TO = process.env.PAY_TO || "acc_BharatNewsNetwork";
const PRICE = process.env.RESOURCE_PRICE || "₹5.00";

// A second, sub-rupee resource. This is the price point that makes the whole
// project's argument: ₹5 clears Razorpay's ₹1 minimum on its own, so without
// this route the console's economics card would always show a zero — nothing
// would ever be "uncollectable per-request" to point at.
const PRICE_MICRO = process.env.RESOURCE_PRICE_MICRO || "₹0.50";

const app = express();
app.use(express.json());

/**
 * CORS.
 *
 * On Vercel the console and both APIs share one origin, which makes this
 * moot in production — but it is what lets the console run against a
 * separately-started resource server in local dev, and lets anyone poke the
 * API from a page of their own. `*` is fine here: nothing behind this gate
 * is cookie-authenticated, and the payment proof itself is the credential.
 *
 * `Access-Control-Expose-Headers` is the part that is easy to miss and silently
 * breaks a browser client: without it, `response.headers.get("payment-required")`
 * returns `null` even on a genuine 402, because the fetch spec hides
 * non-simple response headers from JS unless the server explicitly exposes
 * them. curl and httpx never had this problem, which is why it went unnoticed
 * until a browser was actually in the loop.
 *
 * The OPTIONS short-circuit has to sit before the payment gate — a preflight
 * to a paid route would otherwise be evaluated as a request for payment.
 */
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-PAYMENT, PAYMENT-SIGNATURE");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader(
    "Access-Control-Expose-Headers",
    "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE"
  );
  if (req.method === "OPTIONS") {
    return res.sendStatus(204);
  }
  next();
});

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
 * The console UI. Plain static files, no build step — matches the rest of
 * this repo's zero-tooling ethos. Registered before the payment gate so the
 * dashboard stays reachable even when settlement is down; it reads the
 * publisher's own revenue, which is exactly the thing you want visible when
 * something is wrong.
 */
app.use(express.static(require("path").join(__dirname, "public")));

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

const routes = buildRoutes({
  payTo: PAY_TO,
  price: PRICE,
  priceMicro: PRICE_MICRO,
  facilitatorUrl: FACILITATOR_URL,
});

/**
 * The payment gate, built lazily on the first request rather than at module
 * load or inside `start()`.
 *
 * Neither of those alternatives is right, and both were tried here at
 * different points:
 *
 *   - Built at module load: `paymentMiddleware()` fires its own
 *     `GET /supported` at *construction* time. If the facilitator is not up
 *     yet, that fetch fails and the very first request pays for it — a 502
 *     before the handler ever runs.
 *   - Built inside `start()`: works for `node server.js`, but a serverless
 *     platform imports this module and calls the exported `app` directly.
 *     There is no `start()` call, so the gate would never be built at all,
 *     and every request would need a guard against that — which is exactly
 *     the 503 branch this file used to have.
 *
 * Building on first request sidesteps both: by the time anyone hits this
 * middleware, the module has finished loading and `routes`/`resourceServer`
 * exist, in every environment. The assignment is synchronous, so two
 * concurrent first requests cannot race and build the gate twice.
 *
 * Fail-closed still holds, and it is not this file's job to provide it — the
 * middleware provides it. If `/supported` is unreachable, `requiresPayment()`
 * is still true and `initialize()` throws before the handler runs, so paid
 * content is never served on an unverified request. And per the library's own
 * source (`@x402/express` `initializeHttpServer`), a failed fetch resets its
 * cached promise to `null`, so the *next* request retries rather than staying
 * poisoned — only the unlucky first request ever pays the cold-start cost.
 */
let paymentGate = null;

app.use((req, res, next) => {
  if (!paymentGate) {
    paymentGate = paymentMiddleware(routes, resourceServer);
  }
  return paymentGate(req, res, next);
});

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

/**
 * The micro-priced sibling. Content deliberately looks like a single API
 * lookup rather than a document — that shape, fetched often, is what sub-rupee
 * agent pricing is actually for.
 */
app.get("/premium/api-call", (req, res) => {
  const payer = req.header("x-payment") ? "paid-agent" : "unknown";
  log("premium_resource_served", { resourceId: API_CALL_SAMPLE.resourceId, payer });

  res.json({
    ...API_CALL_SAMPLE,
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

/**
 * Service metadata. Used to live at `GET /`, back when there was no UI to
 * serve there — moved so `express.static` can own the site root and hand
 * visitors the console instead of raw JSON.
 */
app.get("/api/info", (_req, res) => {
  res.json({
    service: "Bharat x402 resource server",
    description: "Publisher content gated behind x402, priced in INR, settled via Razorpay.",
    scheme: SCHEME,
    network: NETWORK,
    facilitator: FACILITATOR_URL,
    routes: {
      free: "/free/market-report-preview",
      paid: "/premium/market-report",
      health: "/health",
      resources: "/api/resources",
    },
  });
});

/**
 * What the console's resource picker fetches at load time, rather than
 * hardcoding prices into the page — the price lives in one place, this
 * service's own env config, the same values the 402 itself quotes.
 */
app.get("/api/resources", (_req, res) => {
  res.json({
    resources: [
      {
        key: "market-report",
        path: "/premium/market-report",
        price: PRICE,
        title: PREMIUM_MARKET_REPORT.title,
        description: "A full report — the kind of thing you fetch once.",
      },
      {
        key: "api-call",
        path: "/premium/api-call",
        price: PRICE_MICRO,
        title: API_CALL_SAMPLE.title,
        description: "A single lookup — the kind of thing an agent calls constantly.",
      },
    ],
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
 * Boots the server for local dev (`node server.js`).
 *
 * This is a courtesy, not a requirement: the payment gate no longer depends
 * on `start()` running at all (see the gate's own comment above), so a
 * serverless platform that imports `app` directly and never calls `start()`
 * is still correctly wired. What this function buys locally is a port that
 * only opens once the facilitator has actually been reached once, so the
 * very first request a person makes doesn't pay the cold-start tax the gate
 * would otherwise absorb silently.
 *
 * @returns {Promise<void>}
 */
async function start() {
  const ready = await waitForFacilitator();

  if (!ready) {
    // Start anyway: better to fail the paid route and keep the free routes up
    // than to take the whole publisher offline because settlement is down.
    console.warn(
      `[resource-server] facilitator at ${FACILITATOR_URL} did not respond — ` +
        "paid routes will fail until it does"
    );
  }

  app.listen(PORT, () => {
    log("server_started", {
      port: PORT,
      facilitator: FACILITATOR_URL,
      facilitatorReady: ready,
      scheme: SCHEME,
      network: NETWORK,
      price: PRICE,
      priceMicro: PRICE_MICRO,
      payTo: PAY_TO,
    });
    console.log(
      `[resource-server] http://localhost:${PORT} — console + paid routes ` +
        `(${PRICE} / ${PRICE_MICRO})`
    );
  });
}

if (require.main === module) {
  start();
}

module.exports = { app, start, waitForFacilitator };
