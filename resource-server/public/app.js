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
}

// -------------------------------------------------------------- the agent

async function runNegotiation({ tamper }) {
  const resource = document.getElementById("resource-select").value;
  setActionButtonsDisabled(true);
  document.getElementById("result").hidden = true;

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

async function refreshDashboard() {
  try {
    const params = new URLSearchParams();
    if (state.scope === "mine") params.set("agentId", state.agentId);

    const summary = await fetchJson(`${state.facilitatorUrl}/ledger/summary?${params}`);
    renderStats(summary);
    renderBatches((summary.batches || []).map(camelizeRow));

    if (state.scope === "mine") {
      await renderActivity();
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
  } catch {
    showOfflineBanner();
  }
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
      return;
    }
    el.innerHTML = data.events
      .map(
        (e) =>
          `<div class="activity-row">` +
          `<span class="activity-row__event">${escapeHtml(EVENT_LABELS[e.event] || e.event)}</span>` +
          `<span class="activity-row__detail">${escapeHtml(e.resourceId || "")}</span>` +
          `<span class="activity-row__amount">${e.amountPaise != null ? paise(e.amountPaise) : ""}</span>` +
          `</div>`
      )
      .join("");
  } catch {
    el.innerHTML = '<p class="muted">Could not load activity.</p>';
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
