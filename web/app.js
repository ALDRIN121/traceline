/* ============================================================================
 * Evaluation Engine — dashboard renderer (web/app.js)
 *
 * A declarative renderer: the page fetches a server-resolved dashboard
 * definition (/api/dashboards/default) and the run list (/runs) — and
 * renders only the blocks the definition declares, instantiating registry
 * components from a closed, fixed map (the component vocabulary mirrors the
 * backend's registry; unknown components degrade to an unavailable card).
 * Nothing here builds layout from arbitrary API data, and nothing here ever
 * ships a definition's payload to innerHTML unescaped.
 *
 * Selection bindings ($selection.case) are UI state. The API resolves
 * dashboard requests per run_id only, so the renderer resolves a selection
 * reference client-side against the sanctioned endpoints the resolver itself
 * points at (GET /runs/{id} for metric rows with evidence ids; GET
 * /runs/{id}/traces/{case} for the full trace stream — §37B.2 rule 6).
 *
 * No telemetry, no third-party anything: every request is a same-origin GET
 * (plus nothing else).
 * ========================================================================== */

"use strict";

/* ---------------------------------------------------------------------------
 * Small utilities
 * ------------------------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.error && body.error.message) detail = body.error.message;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(`GET ${path} → ${detail}`);
  }
  return res.json();
}

const fmtUsd = (usd) => {
  if (usd == null) return "—";
  const abs = Math.abs(usd);
  if (abs >= 1) return `$${usd.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  if (abs >= 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toPrecision(2)}`;
};

const fmtTokens = (n) => (n == null ? "—" : Number(n).toLocaleString());

/* A run id, shortened for tight cells. */
const shortId = (id) => (id && id.length > 12 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id);

/* Metric values: the method names the resolver exposes (pass_rate, mean, p95,
 * count, …). Pass rates are stored as 0..1. */
const fmtMetricValue = (method, value) => {
  if (value == null) return "—";
  if (typeof value === "number") {
    if (method === "pass_rate") return `${(value * 100).toFixed(1)}%`;
    return Number.isInteger(value) ? value.toLocaleString() : String(Number(value.toPrecision(6)));
  }
  return String(value);
};

/* ---------------------------------------------------------------------------
 * Closed vocabulary → presentation mapping.
 * The backend emits exactly these statuses and badges (§37B); the renderer
 * maps them to label + tone and renders nothing else. Unknown values degrade
 * to a neutral labeled chip — the renderer never crashes on new vocabulary.
 * ------------------------------------------------------------------------ */

const TONES = {
  green: "green", red: "red", amber: "amber",
  sky: "sky", violet: "violet", accent: "accent", neutral: "neutral",
};

const RUN_STATUS = {
  draft: { label: "draft", tone: TONES.neutral },
  queued: { label: "queued", tone: TONES.sky },
  provisioning: { label: "provisioning", tone: TONES.sky },
  running: { label: "running", tone: TONES.sky },
  aggregating: { label: "aggregating", tone: TONES.sky },
  complete: { label: "complete", tone: TONES.green },
  failed: { label: "failed", tone: TONES.red },
  cancelled: { label: "cancelled", tone: TONES.amber },
  incomplete: { label: "incomplete", tone: TONES.amber },
};

const CASE_STATUS = {
  queued: { label: "queued", tone: TONES.neutral },
  running: { label: "running", tone: TONES.sky },
  completed: { label: "completed", tone: TONES.green },
  failed: { label: "failed", tone: TONES.red },
  cancelled: { label: "cancelled", tone: TONES.amber },
  skipped: { label: "skipped", tone: TONES.neutral },
};

const METRIC_STATUS = {
  PASS: { label: "PASS", tone: TONES.green },
  FAIL: { label: "FAIL", tone: TONES.red },
  ERROR: { label: "ERROR", tone: TONES.red },
  SKIPPED: { label: "SKIPPED", tone: TONES.neutral },
};

const AGG_STATE = {
  COMPLETE: { label: "COMPLETE", tone: TONES.green },
  PARTIAL: { label: "PARTIAL", tone: TONES.amber },
  IN_PROGRESS: { label: "IN PROGRESS", tone: TONES.sky },
};

const GATE_STATUS = {
  PASS: { label: "gate PASS", tone: TONES.green },
  FAIL: { label: "gate FAIL", tone: TONES.red },
  /* Ungated metrics report NOT_APPLICABLE rather than a gate verdict —
   * present it as informational, never as a pass/fail. */
  NOT_APPLICABLE: { label: "not gated", tone: TONES.neutral },
};

/* Badge flags (closed set from the backend). provisional renders as
 * UNCALIBRATED — the dashboard's own vocabulary maps it (§37B). */
const BADGES = {
  provisional: { label: "UNCALIBRATED", tone: TONES.amber },
  retry: { label: "retry", tone: TONES.sky },
  non_authoritative: { label: "non-authoritative", tone: TONES.neutral },
  overridden: { label: "overridden", tone: TONES.violet },
};

const EVENT_TYPE = {
  llm_call: { label: "llm_call", tone: TONES.violet },
  llm_response: { label: "llm_response", tone: TONES.violet },
  tool_call: { label: "tool_call", tone: TONES.sky },
  tool_result: { label: "tool_result", tone: TONES.sky },
  error: { label: "error", tone: TONES.red },
  retrieval: { label: "retrieval", tone: TONES.green },
  delegation: { label: "delegation", tone: TONES.violet },
  guardrail_check: { label: "guardrail_check", tone: TONES.amber },
  wait_for_input: { label: "wait_for_input", tone: TONES.amber },
  user_response: { label: "user_response", tone: TONES.amber },
  stream_start: { label: "stream_start", tone: TONES.neutral },
  first_token: { label: "first_token", tone: TONES.neutral },
  retry: { label: "retry", tone: TONES.amber },
  budget_exceeded: { label: "budget_exceeded", tone: TONES.red },
};

const REDACTION_STATUS = {
  clean: null,
  redacted: { label: "redacted", tone: TONES.amber },
  truncated: { label: "truncated", tone: TONES.amber },
};

function statusChip(mapping, key, { strong = false, dot = true } = {}) {
  /* null/undefined key → no chip at all; unknown key → neutral labeled chip. */
  if (key == null) return null;
  const m = mapping[key] || { label: String(key), tone: TONES.neutral };
  const chip = el("span", { class: "chip", dataset: { tone: m.tone } });
  if (strong) chip.dataset.strong = "";
  if (dot) chip.append(el("span", { class: "dot" }));
  chip.append(el("span", { text: m.label }));
  return chip;
}

const chip = (label, tone = TONES.neutral, opts = {}) => {
  const c = el("span", { class: "chip", dataset: { tone } });
  if (opts.dot !== false) c.append(el("span", { class: "dot" }));
  c.append(el("span", { text: label }));
  return c;
};

function badgeChips(badges) {
  /* badges[0] is the metric status; the rest are the closed flag set. */
  return (badges || []).slice(1).map((b) => {
    const m = BADGES[b] || { label: String(b), tone: TONES.neutral };
    return chip(m.label, m.tone);
  });
}

/* ---------------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------------ */

const state = {
  definition: null,   // resolved dashboard payload (GET /api/dashboards/default)
  runs: [],           // run summaries (GET /runs)
  filterRun: "latest",// the run filter's current value
  selection: { case: null },
  selectionName: null,// selection id published by a block (e.g. "case")
  runDetail: null,    // cached GET /runs/{id} (evidence assembly)
  traces: null,       // cached GET /runs/{id}/traces/{case}
  polling: false,
  pollTimer: null,
  pollSeq: 0,
  selectionRetryTimer: null, // trace-evidence fetch retry (transient errors)
  activeCase: null,   // case row holding keyboard focus when the grid rebuilt
  lastPhase: null,    // announced run phase (aria-live)
};

/* ---------------------------------------------------------------------------
 * Data loading
 * ------------------------------------------------------------------------ */

async function loadRuns() {
  /* Keyset pagination: walk pages until exhausted (capped defensively). */
  const runs = [];
  let cursor = null;
  for (let page = 0; page < 6; page++) {
    const q = new URLSearchParams({ limit: "500" });
    if (cursor) q.set("cursor", cursor);
    const body = await api(`/runs?${q}`);
    runs.push(...(body.runs || []));
    cursor = body.next_cursor || null;
    if (!cursor) break;
  }
  state.runs = runs;
  renderRunSelector();
}

function dashboardUrl() {
  const q = new URLSearchParams();
  if (state.filterRun !== "latest") q.set("run_id", state.filterRun);
  const s = q.toString();
  return `/api/dashboards/default${s ? `?${s}` : ""}`;
}

async function loadDashboard() {
  const body = await api(dashboardUrl());
  state.definition = body;
  state.selectionName = findSelectionName(body);
  await resolveSelectionData();
  render();
  schedulePolling();
  return body;
}

async function refreshAll() {
  await Promise.all([loadDashboard(), loadRuns()]);
}

/* ---------------------------------------------------------------------------
 * Selection binding ($selection.<id> — resolved client-side)
 *
 * A case_table block publishes a selection through its literal `selection`
 * input (payload rows carry {source, value}); a trace_evidence block whose
 * `case` bind resolved to null is bound to that selection. Clicking a row
 * sets the selection; the renderer then assembles the evidence payload from
 * the same pre-aggregated + trace endpoints the server resolver uses.
 * ------------------------------------------------------------------------ */

function findSelectionName(defn) {
  for (const b of defn.blocks || []) {
    if (b.component === "case_table" && b.bind && typeof b.bind.selection === "string") {
      return b.bind.selection;
    }
  }
  return null;
}

/* Blocks whose case binding is the live selection (currently: trace_evidence
 * blocks with an unbound case input). */
function selectionBoundBlocks(defn) {
  if (!state.selectionName) return [];
  return (defn.blocks || []).filter(
    (b) => b.component === "trace_evidence" && b.bind && b.bind.case == null
  );
}

async function resolveSelectionData() {
  if (!state.selection.case) return;
  const run = state.definition && state.definition.run;
  if (!run) { state.selection.case = null; return; }
  if (!state.runDetail || state.runDetail.run.run_id !== run.run_id) {
    state.runDetail = await api(`/runs/${encodeURIComponent(run.run_id)}`);
    state.traces = null;
  }
  const pair = `${run.run_id}/${state.selection.case}`;
  if (!state.traces || state.traces.pair !== pair) {
    const body = await api(`/runs/${encodeURIComponent(run.run_id)}/traces/${encodeURIComponent(state.selection.case)}`);
    state.traces = { pair, events: body.events || [] };
  }
}

/* Assemble the trace_evidence payload exactly as the server resolver shapes
 * it (dashboard.py::_resolve_trace_evidence) — same keys, same semantics —
 * with the event stream attached for the renderer. */
function assembleEvidencePayload() {
  const defn = state.definition;
  const run = defn.run;
  const caseId = state.selection.case;
  const metrics = [];
  const caseRow = state.runDetail && state.runDetail.cases
    ? state.runDetail.cases.find((c) => c.case_id === caseId)
    : null;
  if (caseRow) {
    const specMetrics = (state.runDetail.run.spec && state.runDetail.run.spec.metrics) || [];
    for (const m of specMetrics) {
      const row = caseRow.metrics.find((r) => r.metric_id === m.metric_id);
      if (!row) continue;
      metrics.push({
        metric_id: row.metric_id,
        status: row.status,
        score: row.score,
        evidence_event_ids: row.evidence_event_ids || [],
        evidence_count: (row.evidence_event_ids || []).length,
        score_revision: row.score_revision,
        is_authoritative: row.is_authoritative,
        on_retry_override: row.on_retry_override,
        overridden: row.overridden,
        judge_binding: row.judge_binding,
        provisional: !!m.provisional,
      });
    }
  }
  const caseName = caseNameFromDefn(caseId);
  /* Only attach the event stream when it belongs to this run/case pair — a
   * failed fetch leaves the previous pair cached, which must not leak. */
  const traces = state.traces && state.traces.pair === `${run.run_id}/${caseId}` ? state.traces : null;
  return {
    component: "trace_evidence",
    run,
    case_id: caseId,
    case_name: caseName,
    metrics,
    placeholder: false,
    metrics_complete: defn.metrics_complete,
    render_final: defn.render_final,
    events: (traces && traces.events) || [],
  };
}

function caseNameFromDefn(caseId) {
  for (const b of state.definition.blocks || []) {
    if (b.component === "case_table") {
      const c = (b.payload.cases || []).find((x) => x.case_id === caseId);
      if (c && c.name) return c.name;
    }
  }
  return caseId;
}

function selectCase(caseId) {
  if (caseId === state.selection.case) return;
  clearSelectionRetry();
  state.selection.case = caseId;
  refreshSelectionBlocks();
}

function clearSelectionRetry() {
  if (state.selectionRetryTimer) {
    clearTimeout(state.selectionRetryTimer);
    state.selectionRetryTimer = null;
  }
}

async function refreshSelectionBlocks() {
  const blocks = selectionBoundBlocks(state.definition);
  if (!blocks.length) return;
  const container = $("#grid");
  if (!container) return;
  for (const b of blocks) {
    const card = container.querySelector(`[data-block="${CSS.escape(b.component + "-" + (b.row || 0) + "-" + (b.col || 0))}"]`);
    if (card) {
      const bodyEl = card.querySelector(".card-body");
      if (bodyEl) {
        bodyEl.replaceChildren(renderLoading("Loading trace evidence…"));
      }
    }
  }
  try {
    await resolveSelectionData();
  } catch (err) {
    /* Transient network error — keep the last rendered payload in place and
     * retry on the poll loop's backoff cadence; a hiccup mid-selection must
     * not take the whole dashboard down (same policy as polling). */
    console.warn("trace evidence fetch failed — retrying:", err);
    render();
    if (state.selectionRetryTimer != null) return;
    state.selectionRetryTimer = setTimeout(() => {
      state.selectionRetryTimer = null;
      if (state.selection.case) refreshSelectionBlocks();
    }, 4000);
    return;
  }
  render();
}

/* ---------------------------------------------------------------------------
 * Renderers — one per registry component
 * ------------------------------------------------------------------------ */

function renderLoading(text) {
  const wrap = el("div", { class: "card-empty" });
  wrap.append(el("p", { text: text || "Loading…" }));
  return wrap;
}

function renderUnavailable(payload) {
  const wrap = el("div", { class: "card-empty" });
  wrap.append(el("p", { class: "empty-text", text: payload.reason || "This block is unavailable." }));
  return wrap;
}

/* ---- metric_summary ------------------------------------------------------ */

function renderMetricSummary(payload) {
  const root = el("div", { class: "stat-tile" });

  const eyebrow = el("div", { class: "stat-eyebrow" });
  eyebrow.append(
    el("span", { text: `${payload.metric_id || "metric"} · ${payload.stat || ""}` }),
    el("span", { class: "chips" }, [
      payload.provisional ? chip("UNCALIBRATED", TONES.amber) : null,
      statusChip(AGG_STATE, payload.aggregation_state, { dot: false }),
      statusChip(GATE_STATUS, payload.gate_status, { dot: false }),
    ]),
  );
  root.append(eyebrow);

  const available = payload.available !== false;
  const value = el("div", { class: "stat-value", dataset: { muted: !available || payload.value == null } });

  if (payload.stat === "cost" && available) {
    value.textContent = fmtUsd(payload.value);
  } else if (available) {
    value.textContent = fmtMetricValue(payload.method, payload.value);
  } else {
    value.textContent = "—";
  }
  root.append(value);

  const sub = [];
  if (payload.stat === "cost") sub.push(`method ${payload.method || "sum"}`);
  else if (payload.method) sub.push(`method ${payload.method}`);
  if (payload.sample_n != null) sub.push(`n=${payload.sample_n}`);
  if (payload.error_n) sub.push(`${payload.error_n} errored`);
  if (payload.skipped_n) sub.push(`${payload.skipped_n} skipped`);
  if (sub.length) root.append(el("div", { class: "stat-sub", text: sub.join(" · ") }));

  if (!available) {
    root.append(el("div", { class: "stat-reason", text: payload.reason || "Not available." }));
  }

  if (payload.stat === "cost" && Array.isArray(payload.cost_breakdown) && payload.cost_breakdown.length) {
    const t = el("table", { class: "cost-table" });
    t.append(el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "price version" }),
        el("th", { text: "input" }),
        el("th", { text: "output" }),
        el("th", { text: "cost" }),
      ]),
    ]));
    const tb = el("tbody");
    let inTotal = 0, outTotal = 0;
    for (const row of payload.cost_breakdown) {
      inTotal += row.input_tokens || 0;
      outTotal += row.output_tokens || 0;
      tb.append(el("tr", {}, [
        el("td", { text: row.price_version || "—" }),
        el("td", { text: fmtTokens(row.input_tokens) }),
        el("td", { text: fmtTokens(row.output_tokens) }),
        el("td", { text: fmtUsd((row.usd_micros || 0) / 1e6) }),
      ]));
    }
    tb.append(el("tr", { class: "total" }, [
      el("td", { text: "total" }),
      el("td", { text: fmtTokens(inTotal) }),
      el("td", { text: fmtTokens(outTotal) }),
      el("td", { text: fmtUsd(payload.value) }),
    ]));
    t.append(tb);
    root.append(t);
  }
  return root;
}

/* ---- run_table ------------------------------------------------------------ */

const CASE_COUNT_ORDER = ["completed", "failed", "cancelled", "skipped", "running", "queued"];
const CASE_COUNT_TONE = {
  completed: TONES.green, failed: TONES.red, cancelled: TONES.amber,
  skipped: TONES.neutral, running: TONES.sky, queued: TONES.neutral,
};

function renderCountsBar(counts) {
  const wrap = el("div", { class: "counts" });
  const total = counts.total || 0;
  const pending = (counts.queued || 0) + (counts.running || 0);

  if (total > 0) {
    const bar = el("div", { class: "counts-bar", role: "img" });
    bar.setAttribute("aria-label", [
      counts.completed && `${counts.completed} completed`,
      counts.failed && `${counts.failed} failed`,
      counts.cancelled && `${counts.cancelled} cancelled`,
      counts.skipped && `${counts.skipped} skipped`,
      counts.running && `${counts.running} running`,
      counts.queued && `${counts.queued} queued`,
    ].filter(Boolean).join(", "));
    for (const key of CASE_COUNT_ORDER) {
      const n = counts[key] || 0;
      if (!n) continue;
      const seg = el("div", { class: "seg", dataset: { tone: CASE_COUNT_TONE[key] } });
      if (key === "running" || key === "queued") seg.dataset.pending = "true";
      seg.style.flexGrow = String(n);
      bar.append(seg);
    }
    wrap.append(bar);
  }

  const legend = el("div", { class: "counts-legend" });
  const totalSpan = el("span", { class: "counts-total" });
  totalSpan.append(el("strong", { text: String(total) }), ` case${total === 1 ? "" : "s"}`);
  legend.append(totalSpan);
  for (const key of CASE_COUNT_ORDER) {
    const n = counts[key] || 0;
    if (!n) continue;
    legend.append(chip(`${key} ${n}`, CASE_COUNT_TONE[key]));
  }
  if (pending > 0) {
    legend.append(el("span", { class: "counts-note", text: `${pending} pending — results are partial` }));
  }
  wrap.append(legend);
  return wrap;
}

function renderRunTable(payload) {
  const root = el("div");

  root.append(renderCountsBar(payload.cases || {}));

  const wrap = el("div", { class: "table-wrap" });
  const t = el("table", { class: "data" });
  const head = el("thead", {}, [
    el("tr", {}, [
      el("th", { text: "Metric" }),
      el("th", { class: "num", text: "Value" }),
      el("th", { text: "Method" }),
      el("th", { text: "Aggregation" }),
      el("th", { text: "Gate" }),
      el("th", { class: "num", text: "n" }),
      el("th", { text: "Flags" }),
    ]),
  ]);
  const tb = el("tbody");
  for (const m of payload.metrics || []) {
    const incomplete = m.aggregation_state !== "COMPLETE";
    const flags = el("span", { class: "chips" }, [
      m.provisional ? chip("UNCALIBRATED", TONES.amber) : null,
      m.no_ci ? chip("no CI", TONES.neutral) : null,
    ]);
    const row = el("tr", {}, [
      el("td", {}, [el("span", { class: "cell-main", text: m.name || m.metric_id })]),
      el("td", { class: "num mono-cell" }, [
        el("span", { class: incomplete ? "muted" : undefined, text: fmtMetricValue(m.method, m.value) }),
      ]),
      el("td", { class: "mono-cell muted", text: m.method || "—" }),
      el("td", {}, [statusChip(AGG_STATE, m.aggregation_state)]),
      el("td", {}, [m.gate_status ? statusChip(GATE_STATUS, m.gate_status) : el("span", { class: "muted", text: "—" })]),
      el("td", { class: "num" }, [
        el("span", { text: String(m.sample_n || 0) }),
        (m.error_n || m.skipped_n)
          ? el("span", { class: "cell-sub", text: `${m.error_n || 0} err · ${m.skipped_n || 0} skip` })
          : null,
      ]),
      el("td", {}, [flags]),
    ]);
    tb.append(row);
  }
  t.append(head, tb);
  wrap.append(t);
  root.append(wrap);
  return root;
}

/* ---- case_table ----------------------------------------------------------- */

function renderCaseTable(payload) {
  const cases = payload.cases || [];
  if (!cases.length) {
    const empty = el("div", { class: "empty" });
    empty.append(svgIcon("table"));
    empty.append(el("p", { class: "empty-title", text: "No cases" }));
    empty.append(el("p", { class: "empty-text", text: "This run has no cases to show." }));
    return empty;
  }

  const metricIds = new Set();
  for (const c of cases) for (const m of c.metrics || []) metricIds.add(m.metric_id);

  const wrap = el("div", { class: "table-wrap" });
  const t = el("table", { class: "data" });
  const head = el("thead", {}, [
    el("tr", {}, [
      el("th", { text: "Case" }),
      el("th", { text: "Status" }),
      el("th", { text: "Classification" }),
      el("th", { text: "Tags" }),
      ...[...metricIds].map((id) => el("th", { text: id })),
    ]),
  ]);
  const tb = el("tbody");

  for (const c of cases) {
    const selectable = !!(c.selection && c.selection.value);
    const tr = el("tr", {
      class: "case-row",
      tabindex: selectable ? "0" : undefined,
      role: "row",
      dataset: { case: c.case_id },
      "aria-selected": selectable && c.case_id === state.selection.case ? "true" : "false",
      title: selectable ? "Select to view trace evidence" : undefined,
    }, [
      el("td", {}, [
        el("span", { class: "cell-main", text: c.name || c.case_id }),
        el("span", { class: "cell-sub", text: c.case_id }),
      ]),
      el("td", {}, [statusChip(CASE_STATUS, c.status)]),
      el("td", {}, [
        c.classification
          ? el("span", { class: "muted", text: String(c.classification) })
          : c.error_category
            ? el("span", { class: "mono-cell", style: "color: var(--red)", text: String(c.error_category) })
            : el("span", { class: "muted", text: "—" }),
      ]),
      el("td", {}, [
        ...(c.tags || []).map((tag) => el("span", { class: "tag", text: tag })),
        (!c.tags || !c.tags.length) ? el("span", { class: "muted", text: "—" }) : null,
      ]),
      ...[...metricIds].map((id) => {
        const m = (c.metrics || []).find((x) => x.metric_id === id);
        if (!m) return el("td", {}, [el("span", { class: "muted", text: "—" })]);
        const cells = el("span", { class: "chips" }, [
          statusChip(METRIC_STATUS, m.status),
          el("span", { class: "mono-cell", text: fmtScore(m) }),
          ...badgeChips(m.badges),
          m.score_revision > 1 ? chip(`rev ${m.score_revision}`, TONES.neutral, { dot: false }) : null,
        ]);
        return el("td", {}, [cells]);
      }),
    ]);

    if (selectable) {
      tr.addEventListener("pointerdown", (ev) => {
        if (ev.button !== 0) return;
        ev.preventDefault();
        selectCase(c.selection.value);
      });
      tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          /* render() rebuilds the grid and would drop focus to the body —
           * remember the row so focus is restored on the same case. */
          state.pendingFocus = c.case_id;
          selectCase(c.selection.value);
        }
      });
    } else {
      tr.style.cursor = "default";
    }
    tb.append(tr);
  }

  t.append(head, tb);
  wrap.append(t);
  return wrap;
}

const fmtScore = (m) => {
  if (m.score == null) return "—";
  const s = Number(m.score);
  return Number.isInteger(s) ? String(s) : String(Number(s.toPrecision(4)));
};

/* ---- trace_evidence -------------------------------------------------------- */

function renderTraceEvidence(payload) {
  const root = el("div");

  /* Placeholder: no case bound (server-resolved payload, or selection not set). */
  if (payload.placeholder) {
    const empty = el("div", { class: "empty" });
    empty.append(svgIcon("cursor"));
    empty.append(el("p", { class: "empty-title", text: "No case selected" }));
    empty.append(el("p", { class: "empty-text", text: "Select a case in the Cases table to view its trace evidence." }));
    if (payload.message) empty.append(el("p", { class: "empty-detail", text: payload.message }));
    return empty;
  }

  const caseName = payload.case_name || payload.case_id;

  /* Per-metric evidence summary. */
  const summary = el("div", { class: "evidence-summary" });
  for (const m of payload.metrics || []) {
    const parts = [statusChip(METRIC_STATUS, m.status)];
    parts.push(el("span", { class: "mono-cell muted", text: `${m.metric_id} · ${fmtScore(m)} · ${m.evidence_count} evidence event${m.evidence_count === 1 ? "" : "s"}` }));
    /* Flags ride the resolved metric rows as on_retry_override /
     * is_authoritative / overridden (dashboard.py::_resolve_trace_evidence);
     * is_authoritative:false alone reads as non-authoritative. */
    if (m.on_retry_override) parts.push(chip(BADGES.retry.label, BADGES.retry.tone, { dot: false }));
    if (m.is_authoritative === false) parts.push(chip(BADGES.non_authoritative.label, BADGES.non_authoritative.tone, { dot: false }));
    if (m.overridden) parts.push(chip(BADGES.overridden.label, BADGES.overridden.tone, { dot: false }));
    if (m.provisional) parts.push(chip("UNCALIBRATED", TONES.amber, { dot: false }));
    if (m.judge_binding) parts.push(chip("judge", TONES.violet, { dot: false }));
    const row = el("div", { style: "display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--hairline);" }, parts);
    summary.append(row);
  }
  root.append(summary);

  /* Trace stream — GET /runs/{id}/traces/{case} (the resolver's own pointer). */
  const events = payload.events || [];
  if (!events.length) {
    const empty = el("div", { class: "empty" });
    empty.append(el("p", { class: "empty-title", text: "No trace events" }));
    empty.append(el("p", { class: "empty-text", text: `Case ${caseName} produced no captured events.` }));
    root.append(empty);
    return root;
  }

  const evidenceIds = new Set();
  for (const m of payload.metrics || []) for (const id of m.evidence_event_ids || []) evidenceIds.add(id);

  const timeline = el("div", { class: "timeline" });
  const sorted = [...events].sort((a, b) => (a.sequence || 0) - (b.sequence || 0) || String(a.timestamp || "").localeCompare(String(b.timestamp || "")));

  for (const ev of sorted) {
    const type = EVENT_TYPE[ev.type] || { label: String(ev.type), tone: TONES.neutral };
    const row = el("div", { class: "tl-row", title: `event ${ev.event_id}` });

    const rail = el("div", { class: "tl-rail" });
    rail.append(el("span", { class: "tl-dot", dataset: { tone: type.tone } }));
    rail.append(el("span", { class: "tl-line" }));
    row.append(rail);

    const body = el("div", {});
    const head = el("div", { class: "tl-head" });
    head.append(el("span", { class: "tl-type", style: `color: var(--${type.tone});`, text: type.label }));
    if (ev.tool) head.append(el("span", { class: "tl-tool", text: String(ev.tool) }));
    if (ev.error && ev.error.type) head.append(el("span", { class: "tl-error", text: `${ev.error.type}: ${ev.error.message || ""}` }));
    const meta = el("span", { class: "tl-meta" });
    if (ev.source && ev.source !== "proxy") meta.append(el("span", { text: `source ${ev.source}` }));
    if (ev.duration_ms != null) meta.append(el("span", { text: `${ev.duration_ms} ms` }));
    if (ev.cost && ev.cost.cost_usd != null) meta.append(el("span", { text: fmtUsd(ev.cost.cost_usd) }));
    const redaction = REDACTION_STATUS[ev.redaction_state && ev.redaction_state.status] || null;
    if (redaction) meta.append(chip(redaction.label, redaction.tone, { dot: false }));
    head.append(meta);
    body.append(head);

    if (evidenceIds.has(ev.event_id)) {
      const evRow = el("div", { style: "margin-top:3px;" }, [chip("evidence", TONES.accent, { dot: false })]);
      body.append(evRow);
    }

    const payloadText = ev.payload != null ? JSON.stringify(ev.payload) : null;
    if (payloadText || ev.payload_ref) {
      const det = el("details", { class: "tl-payload" });
      const summaryText = payloadText
        ? `payload · ${payloadText.length > 120 ? `${payloadText.length} bytes` : "…"}`
        : `payload_ref ${ev.payload_ref}`;
      det.append(el("summary", { text: summaryText }));
      if (payloadText) {
        const truncated = payloadText.length > 2000;
        const shown = truncated ? `${payloadText.slice(0, 2000)}…` : payloadText;
        det.append(el("pre", { text: truncated ? `${shown}\n[truncated in view — ${payloadText.length} bytes total]` : shown }));
      }
      body.append(det);
    }
    row.append(body);
    timeline.append(row);
  }
  root.append(timeline);
  return root;
}

/* ---- tiny inline icons (stroke, currentColor) ------------------------------ */

/* Shape tags the icon parser instantiates (closed set — never free markup). */
const SVG_TAGS = new Set(["path", "rect", "circle", "line", "polyline", "polygon"]);

function svgIcon(name) {
  const shapes = {
    table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/>',
    cursor: '<path d="M4 3l6 16 2.4-5.6L18 11 4 3Z"/><path d="M12.4 13.4 18 18"/>',
    flask: '<path d="M9 3h6M10 3v5l-5.2 9.4A1.8 1.8 0 0 0 6.4 20h11.2a1.8 1.8 0 0 0 1.6-2.6L14 8V3"/><path d="M7.5 15h9"/>',
  }[name] || '<circle cx="12" cy="12" r="8"/>';
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const g = document.createElementNS(ns, "g");
  for (const d of shapes.split("><").map((s) => s.replace("<", "").replace(">", ""))) {
    const tag = d.slice(0, d.indexOf(" "));
    if (!SVG_TAGS.has(tag)) continue;
    const attrs = Object.fromEntries([...d.matchAll(/([a-z]+)="([^"]*)"/g)].map((m) => [m[1], m[2]]));
    const p = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) p.setAttribute(k, v);
    g.append(p);
  }
  svg.append(g);
  return svg;
}

/* ---------------------------------------------------------------------------
 * Block shell + grid
 * ------------------------------------------------------------------------ */

const COMPONENT_TITLES = {
  metric_summary: "Metric",
  run_table: "Run overview",
  case_table: "Cases",
  trace_evidence: "Trace evidence",
};

function blockKey(block) {
  return `${block.component}-${block.row || 0}-${block.col || 0}`;
}

function renderBlock(block) {
  /* A block whose case binding is the live selection gets its payload
   * assembled client-side (assembleEvidencePayload mirrors the server
   * resolver — same keys, evidence ids from the run detail, full stream
   * from the trace endpoint). The server placeholder payload stands in
   * only while no selection is set. */
  let payload = block.payload || {};
  const selectionBound =
    block.component === "trace_evidence" &&
    selectionBoundBlocks(state.definition).includes(block);
  if (selectionBound && state.selection.case) {
    payload = assembleEvidencePayload();
  }
  const title = COMPONENT_TITLES[block.component] || block.component;
  const card = el("section", {
    class: "card",
    dataset: { block: blockKey(block), component: block.component, span: block.span },
  });

  const head = el("div", { class: "card-head" });
  const titleEl = el("h2", { class: "card-title", text: title });
  if (payload.run && payload.run.run_id) {
    titleEl.append(el("span", { class: "run-ref", text: shortId(payload.run.run_id) }));
  }
  head.append(titleEl);

  const headChips = el("span", { class: "chips" });
  if (payload.render_final === false) headChips.append(chip("partial", TONES.amber));
  if (payload.placeholder) headChips.append(chip("unbound", TONES.neutral, { dot: false }));
  head.append(headChips);
  card.append(head);

  const bodyEl = el("div", { class: "card-body" });
  card.append(bodyEl);

  try {
    if (payload.available === false) {
      bodyEl.append(renderUnavailable(payload));
    } else if (payload.placeholder) {
      bodyEl.append(renderTraceEvidence(payload));
    } else {
      const fn = {
        metric_summary: renderMetricSummary,
        run_table: renderRunTable,
        case_table: renderCaseTable,
        trace_evidence: renderTraceEvidence,
      }[block.component];
      if (fn) bodyEl.append(fn(payload));
      else bodyEl.append(renderUnavailable({ reason: `Component "${block.component}" is not in this renderer's registry.` }));
    }
  } catch (err) {
    console.error("block render failed:", block.component, err);
    bodyEl.replaceChildren(renderUnavailable({ reason: `This block could not be rendered (${err.message || err}).` }));
  }
  return card;
}

function renderGrid() {
  const grid = $("#grid");
  const defn = state.definition;
  /* The rebuild replaces every row, which drops :focus with it. Remember which
   * case row had keyboard focus so render() can put it back after the swap. */
  const focused = document.activeElement;
  const focusedRow = focused && focused.closest ? focused.closest("tr.case-row") : null;
  state.activeCase = focusedRow ? focusedRow.dataset.case : null;
  grid.replaceChildren();

  if (!defn || !defn.blocks || !defn.blocks.length) {
    grid.append(renderUnavailable({ reason: "The dashboard definition resolved to no blocks." }));
    return;
  }

  const cols = (defn.layout && defn.layout.columns) || 12;
  grid.style.setProperty("--cols", String(cols));

  for (const block of defn.blocks) {
    const card = renderBlock(block);
    const row = (block.row || 0) + 1;
    const col = (block.col || 0) + 1;
    card.style.gridRow = String(row);
    card.style.gridColumn = `${col} / span ${Math.max(1, block.span || 12)}`;
    grid.append(card);
  }
}

/* ---------------------------------------------------------------------------
 * Filter rendering — the definition's filters are rendered only where the
 * API can actually resolve them. run_selector → ?run_id= (supported). A
 * tag_filter would need server-side resolution the API does not expose, so
 * it is intentionally not rendered as a dead control.
 * ------------------------------------------------------------------------ */

function renderRunSelector() {
  const select = $("#run-select");
  const runs = state.runs;

  const options = [];
  const latest = el("option", { value: "latest", text: "Latest run" });
  options.push(latest);
  for (const r of runs) {
    const name = (r.spec && r.spec.name) || "run";
    options.push(el("option", {
      value: r.run_id,
      text: `${name} · ${shortId(r.run_id)} · ${r.status}`,
      title: `${r.run_id} — created ${r.created_at || "?"}`,
    }));
  }
  select.replaceChildren(...options);

  const matches = runs.some((r) => r.run_id === state.filterRun);
  if (state.filterRun !== "latest" && !matches && runs.length) {
    state.filterRun = "latest";
  }
  select.value = state.filterRun;
  select.disabled = false;

  if (!runs.length) {
    latest.textContent = "Latest run (no runs yet)";
    latest.selected = true;
  }
}

/* ---------------------------------------------------------------------------
 * Banner — the run's render state. render_final: false is never presented
 * as a finished dashboard.
 * ------------------------------------------------------------------------ */

function renderBanner() {
  const banner = $("#banner");
  const defn = state.definition;
  if (!defn) return;

  if (!defn.run) {
    banner.hidden = true;
    state.lastPhase = null; /* a later run must re-announce from scratch */
    return;
  }

  const run = defn.run;
  const status = RUN_STATUS[run.status] || { label: run.status, tone: TONES.neutral };
  const terminal = run.terminal === true;

  let tone = TONES.neutral, title, text;
  if (!terminal) {
    tone = TONES.sky;
    title = `Run ${status.label} — results are updating`;
    text = "This dashboard refreshes automatically while the run executes. Counts and metrics shown now are partial and will change.";
  } else if (!defn.render_final) {
    tone = TONES.amber;
    title = `Run ${status.label} — results are partial`;
    text = defn.metrics_complete
      ? "The run has ended, but the dashboard is not final."
      : "The run has ended, but not every metric row is complete — a half-aggregated run is never rendered as finished.";
  } else {
    tone = TONES.green;
    title = `Run ${status.label} — final results`;
    text = `All ${run.case_count || "scheduled"} cases scored and every metric row is complete.`;
  }

  const phase = `${run.run_id}:${run.status}:${terminal}:${defn.render_final}:${defn.metrics_complete}`;

  banner.dataset.tone = tone;
  banner.hidden = false;
  /* The banner is a live region: only replace its content on a real phase
   * change, or the 2s poll would re-announce the same text endlessly. */
  if (phase === state.lastPhase) return;
  state.lastPhase = phase;
  banner.replaceChildren(
    el("span", { class: "dot", style: "margin-top:5px;flex:none;" }),
    el("div", { class: "banner-body" }, [
      el("p", { class: "banner-title", text: title }),
      el("p", { class: "banner-text", text }),
    ]),
  );
}

/* ---------------------------------------------------------------------------
 * Empty workspace — run: null renders a friendly placeholder; blocks still
 * render their payload reasons beneath it.
 * ------------------------------------------------------------------------ */

function renderEmptyWorkspace() {
  const grid = $("#grid");
  const hero = el("div", { class: "card", style: "grid-row:auto;grid-column:1/-1;" }, [
    el("div", { class: "empty" }, [
      svgIcon("flask"),
      el("p", { class: "empty-title", text: "No runs yet" }),
      el("p", { class: "empty-text", text: "This workspace has no runs. Create one through the API (POST /runs with an entrypoint and a spec), start it, and it will appear here." }),
    ]),
  ]);
  grid.append(hero);
  for (const block of state.definition.blocks || []) {
    const card = renderBlock(block);
    const row = (block.row || 0) + 1;
    const col = (block.col || 0) + 1;
    card.style.gridRow = String(row);
    card.style.gridColumn = `${col} / span ${Math.max(1, block.span || 12)}`;
    grid.append(card);
  }
}

/* ---------------------------------------------------------------------------
 * Polling — auto-refresh while the bound run is non-terminal (2s cadence,
 * simple; the progress SSE stream exists but polling keeps the renderer
 * dependency-free and reconnect-proof).
 * ------------------------------------------------------------------------ */

function schedulePolling() {
  const defn = state.definition;
  const run = defn && defn.run;
  const terminal = !run || run.terminal === true;
  const runId = run && run.run_id;

  if (terminal || !runId) {
    stopPolling();
    setRefreshState(false);
    return;
  }
  if (state.polling && state.pollingRun === runId) return;
  stopPolling();
  state.polling = true;
  state.pollingRun = runId;
  setRefreshState(true);

  const seq = ++state.pollSeq;
  const tick = async () => {
    if (!state.polling || seq !== state.pollSeq) return;
    try {
      const detail = await api(`/runs/${encodeURIComponent(runId)}`);
      await refreshAll();
      if (detail.terminal) {
        stopPolling();
        setRefreshState(false);
      } else if (state.polling) {
        state.pollTimer = setTimeout(tick, 2000);
      }
    } catch (err) {
      /* transient network error — keep the dashboard as-is and retry */
      if (state.polling) state.pollTimer = setTimeout(tick, 4000);
    }
  };
  state.pollTimer = setTimeout(tick, 2000);
}

function stopPolling() {
  state.polling = false;
  state.pollingRun = null;
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollSeq++;
}

function setRefreshState(active) {
  const btn = $("#refresh-btn");
  btn.classList.toggle("is-updating", active);
  if (active) btn.setAttribute("aria-label", "Refreshing automatically while the run executes");
  else btn.removeAttribute("aria-label");
}

/* ---------------------------------------------------------------------------
 * Theme — system by default; manual light/dark override persists.
 * ------------------------------------------------------------------------ */

function initTheme() {
  const order = ["system", "light", "dark"];
  let theme = "system";
  try { theme = localStorage.getItem("eval-theme") || "system"; } catch (_) { /* storage unavailable */ }
  if (!order.includes(theme)) theme = "system";

  const apply = () => {
    document.documentElement.dataset.theme = theme;
    const label = theme === "system" ? "Auto" : theme === "light" ? "Light" : "Dark";
    $("#theme-btn").setAttribute("aria-label", `Theme: ${label} — switch`);
  };
  apply();

  $("#theme-btn").addEventListener("click", () => {
    theme = order[(order.indexOf(theme) + 1) % order.length];
    try { localStorage.setItem("eval-theme", theme); } catch (_) { /* ignore */ }
    apply();
  });
}

/* ---------------------------------------------------------------------------
 * Shell wiring
 * ------------------------------------------------------------------------ */

async function loadHealth() {
  try {
    const h = await api("/health");
    const footer = $(".footer-health");
    footer.dataset.ok = h.status === "ok" ? "true" : "false";
    $("#health-text").textContent = h.status === "ok" ? "API ok" : "API degraded";
  } catch (_) {
    $(".footer-health").dataset.ok = "false";
    $("#health-text").textContent = "API unreachable";
  }
}

function render() {
  const defn = state.definition;
  const boot = $("#boot");
  const fatal = $("#fatal");
  boot.hidden = true;
  fatal.hidden = true;
  if (!defn) return;

  renderBanner();
  document.title = defn.name || "Evaluation Engine — Run dashboard";

  const run = defn.run;
  if (!run) {
    renderEmptyWorkspace();
  } else {
    renderGrid();
    /* keep the selected row in sync across refreshes */
    for (const row of document.querySelectorAll("tr.case-row")) {
      const selected = row.getAttribute("aria-selected") === "true";
      if (selected) row.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  /* First mount done — card-in animation runs once, never again on refresh. */
  $("#grid").classList.add("settled");

  const meta = $("#footer-meta");
  meta.textContent = `${defn.name || "dashboard"} · definition v${defn.version} · registry v${defn.registry_version}`;

  /* Restore keyboard focus on the row the user just activated, or on the row
   * that held focus when the grid was rebuilt (renderGrid recorded it). */
  const focusCase = state.pendingFocus || state.activeCase;
  if (focusCase) {
    const target = document.querySelector(`tr.case-row[data-case="${CSS.escape(focusCase)}"]`);
    state.pendingFocus = null;
    state.activeCase = null;
    if (target) target.focus({ preventScroll: true });
  }
}

function showFatal(err) {
  $("#boot").hidden = true;
  const fatal = $("#fatal");
  fatal.hidden = false;
  $("#fatal-detail").textContent = err && err.message ? err.message : String(err);
  stopPolling();
}

async function init() {
  const urlRun = new URLSearchParams(location.search).get("run_id");
  if (urlRun && urlRun !== "latest") state.filterRun = urlRun;

  $("#refresh-btn").addEventListener("click", () => refreshAll().catch(showFatal));
  $("#fatal-retry").addEventListener("click", () => { location.reload(); });
  $("#run-select").addEventListener("change", (ev) => {
    state.filterRun = ev.target.value;
    state.selection.case = null;
    state.runDetail = null;
    state.traces = null;
    clearSelectionRetry();
    try {
      const url = new URL(location.href);
      if (state.filterRun === "latest") url.searchParams.delete("run_id");
      else url.searchParams.set("run_id", state.filterRun);
      history.replaceState(null, "", url);
    } catch (_) { /* file:// context — ignore */ }
    loadDashboard().catch(showFatal);
  });

  initTheme();

  try {
    await loadRuns();
    renderRunSelector();
    await loadDashboard();
    loadHealth();
  } catch (err) {
    showFatal(err);
  }
}

/* Console-level debugging handle (no runtime cost, no network). Exposes
 * the live state and the renderers for manual inspection in devtools. */
window.__dashboard = {
  get state() { return state; },
  selectCase,
  refreshAll,
  assembleEvidencePayload,
  renderers: { metric_summary: renderMetricSummary, run_table: renderRunTable, case_table: renderCaseTable, trace_evidence: renderTraceEvidence },
};

document.addEventListener("DOMContentLoaded", init);
