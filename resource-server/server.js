/**
 * Bharat x402 — resource server (Phase 0 skeleton).
 *
 * Stands in for an Indian publisher / API provider that wants to charge AI
 * agents per request. Phase 1 puts the x402 payment gate in front of
 * /premium/market-report; for now this only proves the service boots.
 */

require("dotenv").config();
const express = require("express");

const app = express();
const PORT = Number(process.env.PORT || 3402);

app.get("/health", (_req, res) => {
  res.json({ service: "resource-server", status: "ok" });
});

app.get("/", (_req, res) => {
  res.json({
    service: "Bharat x402 resource server",
    note: "Phase 0 skeleton — payment gate arrives in Phase 1.",
    routes: ["/health", "/premium/market-report"],
  });
});

app.get("/premium/market-report", (_req, res) => {
  res.json({ placeholder: true, note: "Will be gated behind HTTP 402 in Phase 1." });
});

app.listen(PORT, () => {
  console.log(`[resource-server] listening on http://localhost:${PORT}`);
});
