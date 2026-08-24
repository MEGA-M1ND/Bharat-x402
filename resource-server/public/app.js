"use strict";

/**
 * Bharat x402 console.
 *
 * One browser = one stable agent identity, chosen once and kept in
 * localStorage. That is a deliberate simplification: `/ledger/summary` and
 * `/settle-batch` scope by a single `agentId`, so if this page let a visitor
 * pick a different sample-crawler label per fetch, "this session" on the
 * dashboard would have to mean "all of the identities I've used", which is a
 * harder and less honest thing to show. One identity per browser makes "this
 * session" exact. Other visitors — other browsers — show up as other agents
 * in the "all visitors" view, which is the multi-agent realism this project
 * is actually about.
 *
 * The agent-side signing (step 3 of the negotiation) never happens here. It
 * happens on the facilitator, in facilitator/demo_trace.py, specifically so
 * the shared HMAC secret never ships to this file. Everything below only
 * ever sees what that endpoint already signed.
 */

const SAMPLE_LABELS = ["perplexity-bot", "gptbot", "claude-web", "gemini-crawler", "bytespider"];

const EVENT_LABELS = {
  offer_issued: "offer quoted",
  payment_verified: "payment verified",
  commitment_recorded: "commitment recorded",
  settlement_replayed: "replay blocked — charged once, not twice",
  payment_verify_rejected: "payment rejected",
  payment_settle_rejected: "settlement rejected",
  batch_settled: "batch settled",
  batch_dry_run: "dry run",
};

const state = {
  sessionId: null,
  agentLabel: null,
  agentId: null,
  facilitatorUrl: null,
  resources: [],
  scope: "mine", // "mine" | "all"
  // The signed commitment from the last successful run, kept so the visitor
  // can re-check it themselves. Cleared on a forged run — see captureProof.
  lastProof: null,
  // Live feed: the highest event id already on screen, and the pending timer.
  // `busy` suppresses polling while a negotiation is mid-flight, so the two
  // are not refreshing the dashboard over each other.
  lastEventId: null,
  pollTimer: null,
  busy: false,
};

// --------------------------------------------------------------- identity

function loadIdentity() {
  let sessionId = localStorage.getItem("bx402_session");
  let agentLabel = localStorage.getItem("bx402_label");

  if (!sessionId) {
    const raw = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Math.random());
    sessionId = raw.replace(/-/g, "").slice(0, 10);
    localStorage.setItem("bx402_session", sessionId);
  }
  if (!agentLabel) {
    agentLabel = SAMPLE_LABELS[Math.floor(Math.random() * SAMPLE_LABELS.length)];
    localStorage.setItem("bx402_label", agentLabel);
  }

  state.sessionId = sessionId;
  state.agentLabel = agentLabel;
}

/**
 * Mirrors facilitator/demo_trace.py's `_build_agent_id` exactly, so this page
 * can display and query its own identity without a round trip just to learn
 * what the server would have called it.
 */
function buildAgentId(sessionId, agentLabel) {
  const session = sessionId.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 12) || "anon";
  const label =
    agentLabel
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, "")
      .replace(/^-+|-+$/g, "")
      .slice(0, 24) || "agent";
  return `agent-${label}-${session}`;
}

// -------------------------------------------------------------- utilities

function paise(n) {
  const v = Number(n) || 0;
  return `₹${(v / 100).toFixed(2)}`;
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

/** snake_case -> camelCase, one level deep. `/ledger/summary`'s nested rows
 * are raw sqlite columns; everything this page builds itself is already
 * camelCase, so this is only ever applied to that one response. */
function camelizeRow(row) {
  const out = {};
  for (const [key, value] of Object.entries(row)) {
    out[key.replace(/_([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  return out;
}

async function fetchJson(url, options) {
  const response = await fetch(url, {
    headers: { Accept: "application/json", ...(options && options.headers) },
    ...options,
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // no JSON body — fine for a 204, a real problem for anything else, which
    // the !response.ok branch below will already flag.
  }
  if (!response.ok) {
    const message = (body && (body.message || body.detail)) || `HTTP ${response.status}`;
    const error = new Error(typeof message === "string" ? message : JSON.stringify(message));
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

// -------------------------------------------------------------------- init

async function init() {
  loadIdentity();
  state.agentId = buildAgentId(state.sessionId, state.agentLabel);
  document.getElementById("my-agent-id").textContent = state.agentId;

  try {
    const info = await fetchJson("/api/info");
    state.facilitatorUrl = info.facilitator;
  } catch {
    showOfflineBanner();
    return;
  }

  await Promise.all([loadResources(), loadStatusChips()]);
  wireButtons();
  await refreshDashboard();
  startLiveFeed();
}

function showOfflineBanner() {
  document.getElementById("banner-offline").hidden = false;
}

async function loadResources() {
  try {
    const data = await fetchJson("/api/resources");
    state.resources = data.resources;
    const select = document.getElementById("resource-select");
    select.innerHTML = data.resources
      .map((r) => `<option value="${r.key}">${escapeHtml(r.title)} — ${escapeHtml(r.price)}</option>`)
      .join("");
    select.addEventListener("change", updateResourceHint);
    updateResourceHint();
  } catch {
    showOfflineBanner();
  }
}

function updateResourceHint() {
  const key = document.getElementById("resource-select").value;
  const resource = state.resources.find((r) => r.key === key);
  document.getElementById("resource-hint").textContent = resource ? resource.description : "";
}

async function loadStatusChips() {
  try {
    const supported = await fetchJson(`${state.facilitatorUrl}/supported`);
    const kind = supported.kinds[0];

    document.getElementById("chip-network").textContent = kind.network;

    const mode = kind.extra.razorpayMode;
    const modeChip = document.getElementById("chip-mode");
    modeChip.textContent = mode === "mock" ? "mock razorpay" : `razorpay ${mode}`;
    modeChip.classList.toggle("chip--warn", mode === "mock");
    modeChip.classList.toggle("chip--good", mode !== "mock");

    document.getElementById("chip-settlement").textContent = `${kind.extra.settlementMode} settlement`;
  } catch {
    showOfflineBanner();
  }
}

function wireButtons() {
  document.getElementById("btn-fetch").addEventListener("click", () => runNegotiation({ tamper: false }));
  document.getElementById("btn-forge").addEventListener("click", () => runNegotiation({ tamper: true }));
  document.getElementById("btn-burst").addEventListener("click", runBurst);
  document.getElementById("btn-settle").addEventListener("click", onSettleClick);
  document.getElementById("btn-verify").addEventListener("click", () => verifyProof({ tamper: false }));
  document
    .getElementById("btn-verify-tamper")
    .addEventListener("click", () => verifyProof({ tamper: true }));

  document.querySelectorAll(".scope-toggle__btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".scope-toggle__btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      state.scope = btn.dataset.scope;
      refreshDashboard();
    });
  });
}

function setActionButtonsDisabled(disabled) {
  ["btn-fetch", "btn-forge", "btn-burst"].forEach((id) => {
    document.getElementById(id).disabled = disabled;
  });
  // Doubles as the live-feed guard: while a run is in flight the poll would
  // be refreshing the dashboard underneath it.
  state.busy = disabled;
}

// -------------------------------------------------------------- the agent

async function runNegotiation({ tamper }) {
  const resource = document.getElementById("resource-select").value;
  setActionButtonsDisabled(true);
  document.getElementById("result").hidden = true;
  // Hidden up front so a failed or forged run cannot leave the previous run's
  // proof sitting there looking like it belongs to this one.
  document.getElementById("card-verify").hidden = true;

  try {
    const run = await fetchJson(`${state.facilitatorUrl}/demo/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: state.sessionId,
        agentLabel: state.agentLabel,
        resource,
        tamper,
      }),
    });
    await renderStepsSequentially(run.steps);
    renderResult(run);
    captureProof(run);
    await refreshDashboard();
  } catch (err) {
    renderFatalError(err);
  } finally {
    setActionButtonsDisabled(false);
  }
}

async function runBurst() {
  setActionButtonsDisabled(true);
  document.getElementById("result").hidden = true;
  document.getElementById("card-verify").hidden = true;

  const stepsEl = document.getElementById("steps");
  stepsEl.innerHTML = "";
  const progress = document.createElement("li");
  progress.className = "step";
  stepsEl.appendChild(progress);
  requestAnimationFrame(() => progress.classList.add("step--in"));

  const total = 20;
  let ok = 0;

  for (let i = 1; i <= total; i += 1) {
    progress.innerHTML =
      `<div class="step__head"><span class="step__num">${i}</span>` +
      `<span class="step__title">Fetching the ₹0.50 API call…</span></div>`;
    try {
      const run = await fetchJson(`${state.facilitatorUrl}/demo/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId: state.sessionId,
          agentLabel: state.agentLabel,
          resource: "api-call",
        }),
      });
      if (run.ok) ok += 1;
    } catch {
      // A single failed fetch (e.g. the per-session rate limit) shouldn't
      // stop the burst — keep going and report what actually landed.
    }
  }

  progress.className = "step step--ok step--in";
  progress.innerHTML =
    `<div class="step__head"><span class="step__num">✓</span>` +
    `<span class="step__title">${ok}/${total} micro-fetches completed</span></div>` +
    `<div class="step__note">Watch the economics card below.</div>`;

  await refreshDashboard();
  setActionButtonsDisabled(false);
}

// ------------------------------------------------------------- step trace

function renderStepsSequentially(steps) {
  const ol = document.getElementById("steps");
  ol.innerHTML = "";
  return steps.reduce(
    (chain, step, i) =>
      chain.then(
        () =>
          new Promise((resolve) => {
            setTimeout(
              () => {
                const li = renderStep(step);
                ol.appendChild(li);
                requestAnimationFrame(() => li.classList.add("step--in"));
                resolve();
              },
              i === 0 ? 0 : 260
            );
          })
      ),
    Promise.resolve()
  );
}

function renderStep(step) {
  const li = document.createElement("li");
  li.className = `step step--${step.status}`;

  const head = document.createElement("div");
  head.className = "step__head";
  head.innerHTML =
    `<span class="step__num">${step.n}</span>` +
    `<span class="step__title">${escapeHtml(step.title)}</span>` +
    `<span class="step__status">${escapeHtml(step.status)}</span>`;
  li.appendChild(head);

  const summary = stepSummary(step);
  if (summary) {
    const s = document.createElement("div");
    s.className = "step__summary mono";
    s.textContent = summary;
    li.appendChild(s);
  }

  if (step.note) {
    const note = document.createElement("div");
    note.className = "step__note";
    note.textContent = step.note;
    li.appendChild(note);
  }

  if (step.request || step.response || step.decoded) {
    const details = document.createElement("details");
    details.className = "step__details";
    const summaryEl = document.createElement("summary");
    summaryEl.textContent = "raw request / response";
    details.appendChild(summaryEl);
    const pre = document.createElement("pre");
    pre.className = "step__pre";
    pre.textContent = JSON.stringify(
      { request: step.request, response: step.response, decoded: step.decoded },
      null,
      2
    );
    details.appendChild(pre);
    li.appendChild(details);
  }

  return li;
}

/** A one-line, scannable fact per step, shown before the raw JSON is expanded. */
function stepSummary(step) {
  try {
    const d = step.decoded;
    if (!d) return "";
    if (step.n === 1 && d.value) {
      const accepted = d.value.accepts && d.value.accepts[0];
      return accepted ? `${accepted.extra.humanAmount} via ${accepted.scheme}` : "";
    }
    if (step.n === 2 && d.offer) {
      return `${d.offer.offerId} — ${paise(d.offer.amountPaise)}`;
    }
    if (step.n === 3 && d.signature) {
      return `${d.signature.slice(0, 20)}…${d.tampered ? "  (tampered)" : ""}`;
    }
    if (step.n === 5 && d.value) {
      return `${d.value.success ? "success" : "failed"} — ${d.value.transaction || d.value.errorReason || ""}`;
    }
    return "";
  } catch {
    return "";
  }
}

function renderResult(run) {
  const el = document.getElementById("result");
  el.hidden = false;

  if (!run.ok) {
    el.className = "result result--error";
    el.textContent = `Rejected: ${run.error || "unknown reason"}`;
    return;
  }

  el.className = "result";
  const content = run.content || {};
  const receiptExtra = (run.receipt && run.receipt.extra) || {};

  let html = `<p class="result__title">✓ Content unlocked</p>`;
  html +=
    `<p class="result__meta">Paid ${paise(run.amountPaise)} · ` +
    `${escapeHtml(receiptExtra.settlementMode || "settled")} · ` +
    `<code>${escapeHtml((run.receipt && run.receipt.transaction) || "")}</code></p>`;

  if (content.title) {
    html += `<p class="result__title" style="font-size:13px">${escapeHtml(content.title)}</p>`;
  }
  if (content.summary) {
    html += `<p class="result__summary">${escapeHtml(content.summary)}</p>`;
  }
  if (Array.isArray(content.findings)) {
    html += `<ul class="result__findings">${content.findings.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`;
  } else if (content.pair) {
    html += `<p class="result__summary"><strong>${escapeHtml(content.pair)}</strong>: ${content.rate}</p>`;
    if (content.note) html += `<p class="result__summary muted">${escapeHtml(content.note)}</p>`;
  }

  el.innerHTML = html;
}

// ------------------------------------------------- verify it in the browser
//
// The point of moving off a shared secret was that the facilitator can check
// an agent's payment and cannot manufacture one. That is a claim about which
// key sits where, and a claim is worth much less than something a sceptic can
// run themselves — so this does the verification client-side, against a public
// key fetched from the facilitator rather than one handed over in the trace.
//
// Ed25519 in WebCrypto is relatively recent (Chrome 137+, Firefox 129+,
// Safari 17+), so every path here degrades to an explanation rather than a
// broken button.

function captureProof(run) {
  const card = document.getElementById("card-verify");
  const step = (run.steps || []).find((s) => s.n === 3 && s.decoded);

  // Only a successful, untampered run leaves something worth verifying. A
  // forged run already failed at step 4 and its signature is *meant* to be bad.
  if (!run.ok || !step || step.decoded.tampered) {
    state.lastProof = null;
    card.hidden = true;
    return;
  }

  state.lastProof = {
    canonicalJson: step.decoded.canonicalJson,
    signature: step.decoded.signature,
    publicKeyFromTrace: step.decoded.agentPublicKey,
    agentId: run.agentId,
  };
  card.hidden = false;
  setVerifyOutput(
    "muted",
    "Runs in your browser with WebCrypto, against the key served from " +
      "<code>/agents/&lt;id&gt;</code>. Nothing here is taken on trust."
  );
}

function setVerifyOutput(kind, html) {
  const el = document.getElementById("verify-out");
  el.className = `verify__out ${kind}`;
  el.innerHTML = html;
}

function b64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function importAgentKey(publicKeyB64) {
  return crypto.subtle.importKey(
    "raw",
    b64ToBytes(publicKeyB64),
    { name: "Ed25519" },
    false,
    ["verify"]
  );
}

async function verifyProof({ tamper }) {
  const proof = state.lastProof;
  if (!proof) return;

  if (!window.isSecureContext || !crypto.subtle) {
    setVerifyOutput(
      "verify--bad",
      "WebCrypto needs a secure context (https or localhost), so this browser can't " +
        "run the check here. The signature is still verified server-side on every payment."
    );
    return;
  }

  setVerifyOutput("muted", "Fetching this agent's public key from the facilitator…");

  let registered;
  try {
    // Deliberately re-fetched rather than reusing the key from the trace: a
    // verification against a key supplied by the same response it is checking
    // would prove nothing.
    const record = await fetchJson(
      `${state.facilitatorUrl}/agents/${encodeURIComponent(proof.agentId)}`
    );
    registered = record.publicKey;
  } catch (err) {
    setVerifyOutput("verify--bad", `Could not fetch the public key: ${escapeHtml(err.message)}`);
    return;
  }

  let key;
  try {
    key = await importAgentKey(registered);
  } catch {
    setVerifyOutput(
      "verify--bad",
      "This browser's WebCrypto doesn't support Ed25519 yet — needs Chrome 137+, " +
        "Firefox 129+, or Safari 17+. The signature is still verified server-side."
    );
    return;
  }

  // Changing the amount is the tamper worth showing: it is exactly the edit
  // someone would make if a commitment could be rewritten after the fact.
  const message = tamper ? cheapenAmount(proof.canonicalJson) : proof.canonicalJson;

  const valid = await crypto.subtle.verify(
    { name: "Ed25519" },
    key,
    b64ToBytes(proof.signature),
    new TextEncoder().encode(message)
  );

  const matchesTrace = registered === proof.publicKeyFromTrace;

  if (!tamper && valid) {
    setVerifyOutput(
      "verify--ok",
      `<strong>✓ Signature valid.</strong> Checked in this browser against ` +
        `<code>${escapeHtml(registered.slice(0, 22))}…</code>, fetched from the ` +
        `facilitator's own registry${matchesTrace ? " and matching the key in the trace above" : ""}. ` +
        `The facilitator holds no private key for this agent, so it could verify this ` +
        `payment but could not have produced it.`
    );
  } else if (tamper && !valid) {
    setVerifyOutput(
      "verify--ok",
      `<strong>✓ Tamper rejected.</strong> The amount owed was rewritten to ` +
        `<code>1 paisa</code> and the same signature no longer verifies. A commitment ` +
        `cannot be edited after it is signed — not by the agent, and not by the ` +
        `facilitator holding it.`
    );
  } else {
    // Neither branch should be reachable; say so plainly rather than quietly
    // rendering a green tick for the wrong outcome.
    setVerifyOutput(
      "verify--bad",
      `<strong>Unexpected result.</strong> tampered=${tamper}, valid=${valid}. ` +
        `That should not happen — please open an issue.`
    );
  }
}

function cheapenAmount(canonicalJson) {
  const cheapened = canonicalJson.replace(/"amountPaise":\d+/, '"amountPaise":1');
  // If the field ever gets renamed, fall back to a byte flip so the button
  // still demonstrates a rejection rather than silently verifying fine.
  return cheapened === canonicalJson ? `${canonicalJson} ` : cheapened;
}

function renderFatalError(err) {
  const ol = document.getElementById("steps");
  const li = document.createElement("li");
  li.className = "step step--failed step--in";
  li.innerHTML =
    `<div class="step__head"><span class="step__num">!</span>` +
    `<span class="step__title">${escapeHtml(err.message || "request failed")}</span></div>`;
  ol.appendChild(li);
  if (err.status === undefined) {
    // A thrown TypeError from fetch itself means a service is unreachable,
    // not that it answered with an error — that's the case the banner is for.
    showOfflineBanner();
  }
}

// -------------------------------------------------------------- dashboard

async function refreshDashboard({ skipActivity = false } = {}) {
  try {
    const params = new URLSearchParams();
    if (state.scope === "mine") params.set("agentId", state.agentId);

    const summary = await fetchJson(`${state.facilitatorUrl}/ledger/summary?${params}`);
    renderStats(summary);
    renderBatches((summary.batches || []).map(camelizeRow));

    // `skipActivity` is for the live poll, which has already appended the new
    // rows with their highlight. Re-rendering the list here would replace
    // them with identical un-highlighted ones and throw away the only visual
    // cue that something just arrived.
    if (state.scope === "mine" && !skipActivity) {
      await renderActivity();
    } else if (state.scope === "mine") {
      // nothing — the poll owns the list this time round
    } else {
      renderTopPayers((summary.byAgent || []).map(camelizeRow));
    }

    document.getElementById("btn-settle").disabled = state.scope !== "mine";

    // Economics stays scoped to this session regardless of the toggle — "why
    // batching" is inherently an argument about *your* traffic, and the
    // burst button above only ever affects this session's own numbers.
    const econRes = await fetchJson(
      `${state.facilitatorUrl}/economics?agentId=${encodeURIComponent(state.agentId)}`
    );
    renderEconomics(econRes.economics);
    renderChargeChart(econRes);
  } catch {
    showOfflineBanner();
  }
}

// ------------------------------------------------------- the charge chart
//
// The economics card states that N charges fall under the gateway minimum.
// This draws it, because the claim is spatial: the floor is a line, and the
// bars that sit to the left of it are revenue with no per-request path at
// all. A reader can check that by looking, which they cannot do with a
// sentence.
//
// Inline SVG and no charting library, matching the rest of this page's
// zero-build approach. It is a handful of rects.

const CHART = { width: 420, height: 132, left: 52, right: 12, top: 10, bottom: 26 };

function renderChargeChart(econRes) {
  const el = document.getElementById("chart-body");
  const rows = econRes.distribution || [];
  const floor = econRes.gatewayMinimumPaise;

  if (!rows.length) {
    el.innerHTML =
      '<p class="muted">Fetch a few resources to see how charge sizes fall either ' +
      "side of the gateway minimum.</p>";
    return;
  }

  const { width, height, left, right, top, bottom } = CHART;
  const plotW = width - left - right;
  const plotH = height - top - bottom;

  // Log scale on the amount axis. Charge sizes here span two orders of
  // magnitude (50 paise to ₹5+), and on a linear axis the sub-rupee bars —
  // the entire point — collapse against the left edge.
  const amounts = rows.map((r) => r.amountPaise);
  const maxAmount = Math.max(...amounts, floor * 2);
  const minAmount = Math.min(...amounts, floor / 2);
  const logMin = Math.log10(Math.max(minAmount, 1));
  const logMax = Math.log10(maxAmount);
  const span = logMax - logMin || 1;
  const xFor = (paiseValue) =>
    left + ((Math.log10(Math.max(paiseValue, 1)) - logMin) / span) * plotW;

  const maxCount = Math.max(...rows.map((r) => r.count));
  const barH = Math.max(6, Math.min(20, plotH / rows.length - 6));

  const floorX = xFor(floor);
  let svg =
    `<svg viewBox="0 0 ${width} ${height}" role="img" ` +
    `aria-label="Charge sizes against the ${paise(floor)} gateway minimum" class="chart">`;

  // The floor, drawn first so bars sit on top of it.
  svg +=
    `<line x1="${floorX.toFixed(1)}" y1="${top - 4}" x2="${floorX.toFixed(1)}" ` +
    `y2="${height - bottom + 4}" class="chart__floor" />` +
    `<text x="${floorX.toFixed(1)}" y="${height - bottom + 18}" ` +
    `class="chart__floor-label" text-anchor="middle">${paise(floor)} floor</text>`;

  rows.forEach((row, i) => {
    const y = top + i * (barH + 6);
    const below = row.amountPaise < floor;
    const barW = Math.max(2, (row.count / maxCount) * (plotW * 0.62));
    // Bars grow rightward from the amount's own position, so a bar's left
    // edge is its price and its length is how often that price was charged.
    const x = xFor(row.amountPaise);

    svg +=
      `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" ` +
      `height="${barH.toFixed(1)}" rx="2" ` +
      `class="chart__bar ${below ? "chart__bar--below" : "chart__bar--above"}" />` +
      `<text x="${left - 6}" y="${(y + barH / 2 + 3.5).toFixed(1)}" ` +
      `class="chart__ylabel" text-anchor="end">${paise(row.amountPaise)}</text>` +
      `<text x="${(x + barW + 5).toFixed(1)}" y="${(y + barH / 2 + 3.5).toFixed(1)}" ` +
      `class="chart__count">×${row.count}</text>`;
  });

  svg += "</svg>";

  const below = rows.filter((r) => r.amountPaise < floor);
  const belowCount = below.reduce((sum, r) => sum + r.count, 0);
  const belowTotal = below.reduce((sum, r) => sum + r.totalPaise, 0);

  const caption = belowCount
    ? `<p class="chart__caption"><strong>${belowCount}</strong> charge${belowCount === 1 ? "" : "s"} ` +
      `sit below the line, worth ${paise(belowTotal)}. Individually, Razorpay will not ` +
      `process any of them.</p>`
    : `<p class="chart__caption muted">Everything here clears the floor. Fetch the ` +
      `${paise(50)} API call to put a bar on the left of the line.</p>`;

  el.innerHTML = svg + caption;
}

function renderStats(summary) {
  document.getElementById("stat-revenue").textContent = paise(summary.totalPaise);
  document.getElementById("stat-requests").textContent = String(summary.requests);
  document.getElementById("stat-agents").textContent = String((summary.byAgent || []).length);
}

function renderTopPayers(rows) {
  document.getElementById("activity-title").textContent = "Top payers";
  const el = document.getElementById("activity-body");
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No traffic yet today.</p>';
    return;
  }
  const body = rows
    .slice(0, 8)
    .map(
      (r) =>
        `<tr><td>${escapeHtml(r.agentId)}</td>` +
        `<td class="num">${r.requests} req</td>` +
        `<td class="num">${paise(r.totalPaise)}</td></tr>`
    )
    .join("");
  el.innerHTML = `<table class="table"><tbody>${body}</tbody></table>`;
}

async function renderActivity() {
  document.getElementById("activity-title").textContent = "Recent activity";
  const el = document.getElementById("activity-body");
  try {
    const data = await fetchJson(
      `${state.facilitatorUrl}/ledger/events?agentId=${encodeURIComponent(state.agentId)}&limit=8`
    );
    if (!data.events.length) {
      el.innerHTML = '<p class="muted">Nothing yet — fetch something on the left.</p>';
      state.lastEventId = null;
      return;
    }
    el.innerHTML = data.events.map(activityRow).join("");
    // Newest first, so the head of the list is the high-water mark the
    // incremental poll continues from.
    state.lastEventId = data.events[0].id;
  } catch {
    el.innerHTML = '<p class="muted">Could not load activity.</p>';
  }
}

function activityRow(e, isNew = false) {
  return (
    `<div class="activity-row${isNew ? " activity-row--new" : ""}">` +
    `<span class="activity-row__event">${escapeHtml(EVENT_LABELS[e.event] || e.event)}</span>` +
    `<span class="activity-row__detail">${escapeHtml(e.resourceId || "")}</span>` +
    `<span class="activity-row__amount">${e.amountPaise != null ? paise(e.amountPaise) : ""}</span>` +
    `</div>`
  );
}

// -------------------------------------------------------- the live feed
//
// `/ledger/events` has taken a `sinceId` since it was written and nothing
// used it — the dashboard only refreshed when you clicked something. So a
// settlement run, or another tab's traffic, appeared only if you happened to
// act again.
//
// Two things keep this from being rude to a serverless deployment:
//
//   * `sinceId` means each poll asks for what is new rather than re-reading
//     the window. The usual answer is an empty list.
//   * It stops entirely while the tab is hidden. A console left open in a
//     background tab overnight would otherwise bill for a poll every few
//     seconds to show nobody anything.

const POLL_INTERVAL_MS = 4000;

function startLiveFeed() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      // Catch up immediately rather than waiting out the interval — a tab
      // being brought forward is exactly when someone wants to see the state.
      pollEvents();
      scheduleNextPoll();
    } else {
      clearTimeout(state.pollTimer);
    }
  });
  scheduleNextPoll();
}

function scheduleNextPoll() {
  clearTimeout(state.pollTimer);
  if (document.visibilityState !== "visible") return;
  state.pollTimer = setTimeout(async () => {
    await pollEvents();
    scheduleNextPoll();
  }, POLL_INTERVAL_MS);
}

async function pollEvents() {
  // Nothing to be incremental from yet, and the "all visitors" view is a
  // different query — let the normal refresh own both.
  if (state.lastEventId == null || state.scope !== "mine" || state.busy) return;

  try {
    const params = new URLSearchParams({
      agentId: state.agentId,
      sinceId: String(state.lastEventId),
      limit: "8",
    });
    const data = await fetchJson(`${state.facilitatorUrl}/ledger/events?${params}`);
    if (!data.events.length) return;

    const el = document.getElementById("activity-body");

    // Clear the marker from the previous batch first. The class outlives its
    // animation, and `refreshDashboard({skipActivity: true})` deliberately
    // does not re-render the list — so without this, every row ever appended
    // by a poll stays flagged as new and the highlight stops meaning
    // "this just arrived".
    el.querySelectorAll(".activity-row--new").forEach((row) =>
      row.classList.remove("activity-row--new")
    );

    // Response is newest-first; reversing means prepending each in turn
    // leaves the newest at the top.
    [...data.events].reverse().forEach((event) => {
      el.insertAdjacentHTML("afterbegin", activityRow(event, true));
    });
    while (el.children.length > 8) el.lastElementChild.remove();

    state.lastEventId = data.events[0].id;

    // Something happened, so the totals and the chart are now stale too.
    await refreshDashboard({ skipActivity: true });
  } catch {
    // A failed poll is not worth surfacing — the next one is four seconds
    // away, and the offline banner already covers a genuinely dead service.
  }
}

function renderBatches(rows) {
  const statusEl = document.getElementById("settlement-status");
  const created = rows.filter((r) => r.status === "created");

  if (!rows.length) {
    statusEl.textContent = "Nothing settled yet.";
  } else {
    const total = created.reduce((sum, r) => sum + r.totalPaise, 0);
    statusEl.textContent = `${created.length} Payment Link${created.length === 1 ? "" : "s"} · ${paise(total)}`;
  }

  const el = document.getElementById("links-body");
  if (!rows.length) {
    el.innerHTML = "";
    return;
  }
  const body = rows
    .map((b) => {
      const label = b.paymentLinkId || (b.status === "failed" ? "failed" : "—");
      const linkText =
        b.paymentLinkId && b.paymentLinkUrl
          ? `<a href="${escapeHtml(b.paymentLinkUrl)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`
          : escapeHtml(label);
      return (
        `<tr><td>${linkText}</td>` +
        `<td class="num">${paise(b.totalPaise)}</td>` +
        `<td class="num">${b.commitmentCount} req</td></tr>`
      );
    })
    .join("");
  el.innerHTML = `<table class="table"><tbody>${body}</tbody></table>`;
}

function renderEconomics(econ) {
  const el = document.getElementById("economics-body");
  if (!econ) {
    el.innerHTML = '<p class="muted">Fetch the ₹0.50 API call a few times to see this fill in.</p>';
    return;
  }

  const charges = econ.commitmentCount - econ.gatewayCallsSaved;
  const unreachable = econ.revenueUnreachablePerRequestPaise;

  let html = `<p class="econ-headline"><span class="num">${econ.commitmentCount}</span> requests collected in <span class="num">${charges}</span> gateway charge${charges === 1 ? "" : "s"}.</p>`;

  if (unreachable > 0) {
    html +=
      `<p class="econ-headline">${paise(unreachable)} of ${paise(econ.totalPaise)} could not have been ` +
      `collected per-request — ${econ.belowGatewayMinimum} charge${econ.belowGatewayMinimum === 1 ? "" : "s"} ` +
      `sit under Razorpay's ${paise(econ.gatewayMinimumPaise)} minimum.</p>`;
  } else {
    html +=
      `<p class="econ-row">Every charge here clears the ${paise(econ.gatewayMinimumPaise)} gateway minimum — ` +
      `batching saves API calls and reconciliation rather than fees.</p>`;
  }

  html += `<div class="econ-quote">“${escapeHtml(econ.note)}”</div>`;
  el.innerHTML = html;
}

async function onSettleClick() {
  const btn = document.getElementById("btn-settle");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Settling…";

  try {
    await fetchJson(`${state.facilitatorUrl}/settle-batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agentId: state.agentId }),
    });
    await refreshDashboard();
  } catch (err) {
    document.getElementById("settlement-status").textContent = `Settlement failed: ${err.message}`;
  } finally {
    btn.textContent = original;
    btn.disabled = state.scope !== "mine";
  }
}

document.addEventListener("DOMContentLoaded", init);
