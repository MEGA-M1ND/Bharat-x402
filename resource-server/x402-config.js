/**
 * Bharat x402 — the INR payment scheme, and the routes it protects.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * ---------------------------------------------------------------------------
 * x402 is deliberately facilitator-agnostic. The spec separates three things:
 *
 *   1. a *scheme*      — how a payment is expressed and proven ("exact", here "razorpay-inr")
 *   2. a *network*     — where it settles ("eip155:8453" for Base, here "razorpay:inr-test")
 *   3. a *facilitator* — the service that verifies and settles it
 *
 * The reference stack fills those in with EIP-3009 signatures, USDC, and
 * Coinbase's facilitator. None of that is baked into the protocol. `Network` in
 * @x402/core is literally typed `${string}:${string}`, and both `FacilitatorClient`
 * and `SchemeNetworkServer` are open interfaces.
 *
 * So this file is not a reimplementation of x402 — it is a *plugin* for it. We
 * register a scheme that prices in Indian paise and hand the real middleware a
 * facilitator client pointed at our own FastAPI service. Everything else — the
 * 402 response shape, the X-PAYMENT header, the verify-then-settle ordering —
 * is the stock library doing its normal job.
 *
 * ---------------------------------------------------------------------------
 * WHAT IS SIMPLIFIED FOR THE DEMO  (see docs/architecture.md)
 * ---------------------------------------------------------------------------
 * - Payment proofs are HMAC-signed with a secret shared between agent,
 *   resource server, and facilitator. The real spec expects asymmetric
 *   signatures the agent alone can produce. HMAC means any holder of the shared
 *   secret can forge a commitment; it is fine for a demo, not for production.
 * - `payTo` is an opaque merchant-account string, not a validated Razorpay
 *   account id.
 * - There is no replay cache in the resource server; the facilitator owns
 *   nonce uniqueness.
 */

"use strict";

/** Our scheme name. Stock x402 uses "exact"; we are a different payment mechanism. */
const SCHEME = "razorpay-inr";

/**
 * CAIP-style network id. `razorpay` is the namespace (an off-chain settlement
 * rail rather than a blockchain), `inr-test` the reference (test-mode rupees).
 */
const NETWORK = process.env.X402_NETWORK || "razorpay:inr-test";

/**
 * The "asset" being transferred. For USDC this is a contract address; for us it
 * is a currency code, in ISO 4217 form.
 */
const ASSET = "INR";

/**
 * INR has two decimal places, so amounts on the wire are in **paise**, the way
 * USDC amounts travel in atomic 1e-6 units. ₹5.00 → "500".
 *
 * Keeping money in integer paise everywhere means no float ever touches an
 * amount — the same discipline any real payments system needs.
 */
const INR_DECIMALS = 2;
const PAISE_PER_RUPEE = 100;

/**
 * Turns a human price into integer paise.
 *
 * Accepts, following the x402 `Money` convention where the display unit is the
 * natural one ("$0.10" means ten cents):
 *   "₹5"      → 500
 *   "₹5.50"   → 550
 *   "Rs. 5"   → 500
 *   5         → 500   (a bare number is rupees)
 *   "5"       → 500
 *
 * @param {string|number} price Human-facing price in rupees.
 * @returns {string} Amount in paise, as a decimal string.
 */
function rupeesToPaise(price) {
  const raw = String(price).trim().replace(/^(₹|Rs\.?|INR)\s*/i, "");

  if (!/^\d+(\.\d{1,2})?$/.test(raw)) {
    throw new Error(
      `Cannot parse "${price}" as an INR price. Use forms like "₹5", "₹5.50", or 5.`
    );
  }

  const [rupees, fraction = ""] = raw.split(".");
  const paise = BigInt(rupees) * BigInt(PAISE_PER_RUPEE) + BigInt(fraction.padEnd(2, "0"));
  return paise.toString();
}

/**
 * Formats integer paise back into a rupee string for humans and log lines.
 *
 * @param {string|number|bigint} paise Amount in paise.
 * @returns {string} e.g. "₹5.00"
 */
function paiseToRupeeString(paise) {
  const value = BigInt(paise);
  const rupees = value / BigInt(PAISE_PER_RUPEE);
  const fraction = (value % BigInt(PAISE_PER_RUPEE)).toString().padStart(2, "0");
  return `₹${rupees}.${fraction}`;
}

/**
 * The INR scheme, server side.
 *
 * Implements @x402/core's `SchemeNetworkServer`. The resource server calls this
 * to price a route and to describe the payment it wants; the facilitator holds
 * the matching verify/settle logic. Registering it is what lets the stock
 * middleware emit a 402 denominated in rupees.
 */
class RazorpayInrScheme {
  /**
   * @param {object} options
   * @param {string} options.facilitatorUrl Public URL of our INR facilitator.
   */
  constructor({ facilitatorUrl }) {
    this.scheme = SCHEME;
    this.facilitatorUrl = facilitatorUrl;
  }

  /**
   * Converts a route's configured price into the scheme's atomic amount + asset.
   *
   * @param {string|number|{asset: string, amount: string}} price Route price.
   * @param {string} _network Network id (unused — we have one network).
   * @returns {Promise<{asset: string, amount: string, extra: object}>}
   */
  async parsePrice(price, _network) {
    // An explicit { asset, amount } pair is already in atomic units; trust it.
    if (price && typeof price === "object" && "amount" in price) {
      if (price.asset && price.asset !== ASSET) {
        throw new Error(`${SCHEME} settles ${ASSET} only, got asset "${price.asset}".`);
      }
      return { asset: ASSET, amount: String(price.amount), extra: price.extra || {} };
    }

    const amountPaise = rupeesToPaise(price);
    return {
      asset: ASSET,
      amount: amountPaise,
      extra: {
        currency: ASSET,
        humanAmount: paiseToRupeeString(amountPaise),
      },
    };
  }

  /**
   * Decimal precision of the asset. Used by the library when converting
   * display-unit settlement overrides into atomic units.
   *
   * @returns {number} 2 — rupees to paise.
   */
  getAssetDecimals() {
    return INR_DECIMALS;
  }

  /**
   * Final shaping of the payment requirement advertised in the 402 body.
   *
   * `supportedKind.extra` is whatever our facilitator returned from
   * `GET /supported`, so the settlement mode the client sees is the mode the
   * facilitator is actually running in — not something hardcoded here.
   *
   * @param {object} paymentRequirements Base requirement (amount/asset/payTo already set).
   * @param {object} supportedKind The facilitator's advertised capability for this scheme+network.
   * @param {string[]} _facilitatorExtensions Extensions the facilitator supports.
   * @returns {Promise<object>} The requirement clients receive in `accepts[]`.
   */
  async enhancePaymentRequirements(paymentRequirements, supportedKind, _facilitatorExtensions) {
    const facilitatorExtra = supportedKind.extra || {};

    return {
      ...paymentRequirements,
      extra: {
        ...paymentRequirements.extra,

        // Human-readable mirror of `amount`, so a developer eyeballing the 402
        // does not have to divide by 100 in their head.
        currency: ASSET,
        amountPaise: paymentRequirements.amount,
        humanAmount: paiseToRupeeString(paymentRequirements.amount),

        // Where to get a signed offer. Stock x402 clients construct the payload
        // themselves from a wallet; an INR agent has no wallet, so it asks the
        // facilitator to quote it first.
        facilitatorUrl: this.facilitatorUrl,
        offerEndpoint: `${this.facilitatorUrl}/offer`,

        // "deferred" means this request is booked as a commitment now and
        // charged in one batched Razorpay Payment Link later. See
        // docs/architecture.md for why per-request settlement of ₹5 does not work.
        settlementMode: facilitatorExtra.settlementMode || "unknown",
        settlementRail: facilitatorExtra.settlementRail || "razorpay",

        // Signalling the substitution honestly rather than implying EVM parity.
        proofScheme: facilitatorExtra.proofScheme || "hmac-sha256",
      },
    };
  }
}

/**
 * The gated content. In a real deployment this is a database read or an
 * upstream API call; here it is a fixed document so the demo is deterministic.
 */
const PREMIUM_MARKET_REPORT = {
  resourceId: "market-report-2026-08",
  title: "India Digital Payments Market Report — August 2026",
  publisher: "Bharat News Network",
  generatedAt: "2026-08-11T00:00:00Z",
  summary:
    "UPI processed 19.8B transactions in July 2026, up 41% YoY. Agent-initiated " +
    "API traffic to Indian publishers grew ~6x over the same period, almost all " +
    "of it unmonetised.",
  findings: [
    "Tier-2 and tier-3 cities now account for 62% of new UPI merchant onboarding.",
    "Median API call value for AI crawler traffic sits near ₹4 — far below the " +
      "point where per-transaction card settlement is economic.",
    "Publishers report 3-8% of total origin bandwidth is now agent traffic.",
  ],
  licence: "Single-agent, single-fetch. Not for redistribution.",
};

/**
 * Routes the payment middleware guards, keyed by path.
 *
 * The `accepts` array is the menu of ways to pay. We advertise exactly one —
 * INR through Razorpay. A production publisher could list USDC-on-Base
 * alongside it and let the agent choose; that is the whole point of `accepts`
 * being a list.
 *
 * @param {object} options
 * @param {string} options.payTo Publisher's settlement account.
 * @param {string} options.price Route price in rupees.
 * @returns {object} RoutesConfig for @x402/express.
 */
function buildRoutes({ payTo, price, facilitatorUrl }) {
  const amountPaise = rupeesToPaise(price);

  return {
    "GET /premium/market-report": {
      accepts: [
        {
          scheme: SCHEME,
          network: NETWORK,
          payTo,
          price,
          // How long the agent has to present payment before the offer goes stale.
          maxTimeoutSeconds: 300,
        },
      ],
      description: PREMIUM_MARKET_REPORT.title,
      mimeType: "application/json",
      serviceName: PREMIUM_MARKET_REPORT.publisher,
      tags: ["market-data", "india", "fintech"],

      /**
       * Body returned alongside the 402.
       *
       * Worth being precise about where the protocol data actually lives: in
       * x402 v2 the machine-readable `accepts[]` travels base64-encoded in the
       * `PAYMENT-REQUIRED` response header, and the body is whatever the
       * publisher wants. A conforming client reads the header.
       *
       * We mirror the offer into the body anyway, because a developer running
       * `curl` should be able to see what they are being charged without
       * base64-decoding a header first. The header remains authoritative; the
       * numbers below come from the same `rupeesToPaise` helper the middleware
       * uses, so the two cannot drift.
       *
       * @returns {{contentType: string, body: object}}
       */
      unpaidResponseBody: () => ({
        contentType: "application/json",
        body: {
          error: "payment_required",
          message:
            `Paid resource. ${price} per fetch, settled in INR via Razorpay. ` +
            "Request a signed offer from the facilitator's /offer endpoint, then " +
            "retry this request with an X-PAYMENT header.",

          // Human-readable mirror of the PAYMENT-REQUIRED header.
          offer: {
            resourceId: PREMIUM_MARKET_REPORT.resourceId,
            scheme: SCHEME,
            network: NETWORK,
            asset: ASSET,
            amount: amountPaise,
            amountPaise,
            humanAmount: paiseToRupeeString(amountPaise),
            payTo,
            facilitatorUrl,
            offerEndpoint: `${facilitatorUrl}/offer`,
            maxTimeoutSeconds: 300,
          },

          preview: {
            title: PREMIUM_MARKET_REPORT.title,
            publisher: PREMIUM_MARKET_REPORT.publisher,
            summary: PREMIUM_MARKET_REPORT.summary.slice(0, 120) + "…",
          },

          hint: "The authoritative offer is the base64 PAYMENT-REQUIRED response header.",
          docs: "https://github.com/MEGA-M1ND/Bharat-x402",
        },
      }),
    },
  };
}

module.exports = {
  SCHEME,
  NETWORK,
  ASSET,
  INR_DECIMALS,
  RazorpayInrScheme,
  PREMIUM_MARKET_REPORT,
  buildRoutes,
  rupeesToPaise,
  paiseToRupeeString,
};
