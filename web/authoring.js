/* Review-first authoring workspace. Persistent objects remain server-owned;
 * local storage holds only an unsaved browser draft so a failed request or
 * refresh cannot erase a person's selections. */
(function () {
  "use strict";

  const STORAGE_KEY = "traceline.authoring-draft.v1";
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const params = new URLSearchParams(location.search);
  const sessionId = params.get("session_id");
  const tabs = ["reference", "metrics", "dataset", "preview"];
  let active = "reference";

  const emptyDraft = () => ({ selected: [], customIntent: "", onMissing: "", onError: "" });
  function loadDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      return draft && Array.isArray(draft.selected) ? { ...emptyDraft(), ...draft } : emptyDraft();
    } catch (_) {
      return emptyDraft();
    }
  }
  let draft = loadDraft();

  function saveDraft(message = "Draft saved in this browser") {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    const status = $("#authoring-save-state");
    if (status) status.textContent = message;
  }

  function selected() {
    return all(".metric-option input:not(:disabled):checked").map((input) => input.value);
  }

  function updateSummary() {
    const values = selected();
    draft.selected = values;
    const count = $("#metric-selection-count");
    if (count) count.textContent = `${values.length} selected`;
    const save = $("#save-metric-decisions");
    if (save) save.disabled = values.length === 0 || !draft.onMissing || !draft.onError;
    const inspect = $("#definition-inspector");
    if (inspect && !inspect.hidden) inspect.textContent = definitionText();
  }

  function definitionText() {
    return JSON.stringify({
      proposal: "metric selection",
      selected_metric_ids: selected(),
      custom_intent: draft.customIntent || null,
      on_missing: draft.onMissing || "required",
      on_error: draft.onError || "required",
      execution: "not_requested",
    }, null, 2);
  }

  function setTab(next, focus = false) {
    if (!tabs.includes(next)) return;
    active = next;
    for (const name of tabs) {
      const button = $(`#authoring-${name}-tab`);
      const pane = $(`#authoring-${name}`);
      const isActive = name === next;
      if (button) {
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-selected", String(isActive));
        button.tabIndex = isActive ? 0 : -1;
      }
      if (pane) pane.hidden = !isActive;
    }
    if (focus) $(`#authoring-${next}-tab`)?.focus();
  }

  async function request(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    let body = options.body;
    if (body && typeof body === "object") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const response = await fetch(path, { ...options, headers, body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload?.error?.message || "The saved authoring session could not be reached.");
      error.code = payload?.error?.code;
      throw error;
    }
    return payload;
  }

  function operationKey() {
    return `authoring-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function resumeSession() {
    const binding = $("#authoring-binding-status");
    if (!sessionId) return;
    if (binding) binding.textContent = "Loading saved authoring session…";
    try {
      const session = await request(`/api/sessions/${encodeURIComponent(sessionId)}`);
      if (binding) binding.textContent = `Resumed session · revision ${session.revision}`;
      const saveState = $("#authoring-save-state");
      if (saveState) saveState.textContent = "Saved session restored";
      window.__tracelineAuthoringSession = session;
    } catch (error) {
      if (binding) binding.textContent = `Saved session unavailable: ${error.message}`;
    }
  }

  async function persistMetricProposal() {
    const help = $("#metric-policy-help");
    saveDraft("Metric choices saved locally; waiting for a persisted session");
    const session = window.__tracelineAuthoringSession;
    if (!session) {
      if (help) help.textContent = "Draft retained. Attach a persisted session before submitting these choices to the evaluation service.";
      return;
    }
    const button = $("#save-metric-decisions");
    if (button) button.disabled = true;
    if (help) help.textContent = "Saving the reviewed proposal…";
    try {
      const queued = await request(`/api/sessions/${encodeURIComponent(session.session_id)}/turns`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: {
          expected_revision: session.revision,
          card_action: {
            action: "select_metrics",
            selected_metric_ids: selected(),
            on_missing: draft.onMissing,
            on_error: draft.onError,
            custom_intent: draft.customIntent || null,
          },
        },
      });
      if (help) help.textContent = `Proposal ${queued.state}. The service will report the observed result before this draft changes.`;
      saveDraft("Reviewed proposal queued");
    } catch (error) {
      if (help) help.textContent = `The proposal was not saved: ${error.message}. Your local draft is retained.`;
    } finally {
      updateSummary();
    }
  }

  function wireTabs() {
    all(".authoring-tab").forEach((button) => {
      button.addEventListener("click", () => setTab(button.id.replace("authoring-", "").replace("-tab", "")));
      button.addEventListener("keydown", (event) => {
        if (!(["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))) return;
        event.preventDefault();
        const current = tabs.indexOf(active);
        const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        setTab(tabs[index], true);
      });
    });
  }

  function wireMetrics() {
    all(".metric-option input:not(:disabled)").forEach((input) => {
      input.checked = draft.selected.includes(input.value);
      input.addEventListener("change", () => { updateSummary(); saveDraft(); });
    });
    const intent = $("#authoring-custom-intent");
    if (intent) {
      intent.value = draft.customIntent;
      intent.addEventListener("input", () => { draft.customIntent = intent.value; saveDraft(); updateSummary(); });
    }
    const missing = $("#metric-on-missing");
    const error = $("#metric-on-error");
    if (missing) {
      missing.value = draft.onMissing;
      missing.addEventListener("change", () => { draft.onMissing = missing.value; saveDraft(); updateSummary(); });
    }
    if (error) {
      error.value = draft.onError;
      error.addEventListener("change", () => { draft.onError = error.value; saveDraft(); updateSummary(); });
    }
    $("#review-selected-metrics")?.addEventListener("click", () => {
      const review = $("#metric-review-card");
      if (selected().length === 0) {
        const count = $("#metric-selection-count");
        if (count) count.textContent = "Choose at least one metric";
        return;
      }
      review.hidden = false;
      $("#metric-on-missing")?.focus();
      updateSummary();
    });
    $("#close-metric-review")?.addEventListener("click", () => {
      $("#metric-review-card").hidden = true;
      setTab("metrics");
      $("#review-selected-metrics")?.focus();
    });
    $("#save-metric-decisions")?.addEventListener("click", persistMetricProposal);
    $("#inspect-definition")?.addEventListener("click", (event) => {
      const inspector = $("#definition-inspector");
      if (!inspector) return;
      inspector.hidden = !inspector.hidden;
      inspector.textContent = definitionText();
      event.currentTarget.setAttribute("aria-expanded", String(!inspector.hidden));
      event.currentTarget.textContent = inspector.hidden ? "Inspect definition" : "Hide definition";
    });
    updateSummary();
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireTabs();
    wireMetrics();
    all("[data-authoring-next]").forEach((button) => button.addEventListener("click", () => {
      const target = button.dataset.authoringNext;
      setTab(target === "dataset" ? "dataset" : target === "source" || target === "review" ? "reference" : target);
    }));
    resumeSession();
  });
}());
