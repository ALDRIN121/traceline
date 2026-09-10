/* Review-first authoring workspace. Persistent objects remain server-owned;
 * local storage holds only an unsaved browser draft so a failed request or
 * refresh cannot erase a person's selections. */
(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const params = new URLSearchParams(location.search);
  const sessionId = params.get("session_id");
  const STORAGE_KEY = `traceline.authoring-draft.v1:${sessionId || "unsaved"}`;
  const tabs = ["reference", "metrics", "dataset", "preview"];
  let active = "reference";
  let projectId = (params.get("project_id") || "").trim();
  let knowledgeReport = null;
  let knowledgeContextVersion = 0;
  let knowledgeRequestToken = 0;

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
    // Only plain records are JSON API payloads. File/Blob bodies must reach the
    // upload endpoint unchanged so the server can validate the archive bytes.
    if (body && Object.prototype.toString.call(body) === "[object Object]") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const response = await fetch(path, { ...options, headers, body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload?.error?.message || "The saved authoring session could not be reached.");
      error.code = payload?.error?.code;
      error.correlationId = payload?.error?.correlation_id;
      throw error;
    }
    return payload;
  }

  function operationKey() {
    return `authoring-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function reportError(prefix, error) {
    const correlation = error.correlationId ? ` (correlation ${error.correlationId})` : "";
    return `${prefix}: ${error.message}${correlation}`;
  }

  function setActivity(message) {
    const status = $("#authoring-activity-status");
    if (status) status.textContent = message;
  }

  function setProjectContext(value) {
    projectId = value.trim();
    const input = $("#authoring-project-id");
    const load = $("#load-project-report");
    const importButton = $("#queue-source-import");
    const state = $("#knowledge-state");
    if (input && input.value !== projectId) input.value = projectId;
    if (load) load.disabled = !projectId;
    if (importButton) importButton.disabled = !projectId;
    if (!knowledgeReport && state) state.textContent = projectId ? "Source report not loaded" : "Project context required";
    const sourceStatus = $("#source-import-status");
    if (!projectId && sourceStatus) sourceStatus.textContent = "Choose a project before importing a source.";
  }

  function clearRenderedKnowledgeReport() {
    const card = $("#knowledge-report-card");
    if (card) card.hidden = true;
    $("#knowledge-report-meta")?.replaceChildren();
    $("#knowledge-review-state")?.replaceChildren();
    $("#knowledge-questions")?.replaceChildren();
    $("#knowledge-facts")?.replaceChildren();
  }

  function clearKnowledgeReport() {
    knowledgeReport = null;
    knowledgeContextVersion += 1;
    clearRenderedKnowledgeReport();
  }

  function factStatusLabel(status) {
    return {
      observed_static: "Found in source",
      user_confirmed: "User confirmed",
      observed_execution: "Observed in execution",
      inferred: "Inferred",
      unknown: "Unknown",
      contradicted: "Contradicted",
      review_needed: "Review needed",
    }[status] || status || "Unknown";
  }

  function appendText(parent, tag, text, className = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.append(element);
    return element;
  }

  function renderKnowledgeReport(report) {
    knowledgeReport = report;
    const card = $("#knowledge-report-card");
    const state = $("#knowledge-state");
    const meta = $("#knowledge-report-meta");
    const review = $("#knowledge-review-state");
    const questions = $("#knowledge-questions");
    const facts = $("#knowledge-facts");
    if (!card || !meta || !review || !questions || !facts) return;
    card.hidden = false;
    if (state) state.textContent = report.review_needed ? "Review needed" : `Report revision ${report.revision}`;
    meta.textContent = `Source revision ${report.source_version_id} · report ${report.report_id}`;
    review.textContent = report.review_needed
      ? "A source change affects confirmed findings. Review each fact before using it in authoring."
      : "Facts retain their evidence status. Confirming one creates a new report revision.";
    questions.replaceChildren();
    const pendingQuestions = report.pending_questions || [];
    if (report.needs_entrypoint_declaration || pendingQuestions.length > 0) {
      const callout = document.createElement("div");
      callout.className = "authoring-callout knowledge-question-callout";
      if (report.needs_entrypoint_declaration) {
        appendText(callout, "strong", "Entrypoint declaration required");
        appendText(callout, "p", "The service could not identify a command that invokes this agent.");
      }
      if (pendingQuestions.length > 0) {
        const list = document.createElement("ul");
        list.className = "knowledge-question-list";
        for (const question of pendingQuestions) {
          const item = document.createElement("li");
          appendText(item, "span", question.text || question.question || "The service needs more information.");
          list.append(item);
        }
        callout.append(list);
      }
      appendText(callout, "p", "Answering these questions is unavailable in the current API.");
      questions.append(callout);
    }
    facts.replaceChildren();
    for (const fact of report.facts || []) {
      const item = document.createElement("article");
      item.className = "reference-card knowledge-fact";
      const badge = appendText(item, "span", factStatusLabel(fact.status), `fact-status ${fact.status === "user_confirmed" ? "found" : "unknown"}`);
      badge.dataset.status = fact.status || "unknown";
      appendText(item, "h4", fact.name || "Unnamed finding");
      appendText(item, "p", `${fact.kind || "finding"} · ${fact.summary || "No summary supplied."}`);
      const evidence = (fact.evidence || [])[0];
      if (evidence?.path) appendText(item, "p", `${evidence.path}:${evidence.line_start || "?"}`, "knowledge-citation mono");
      if (fact.status !== "user_confirmed") {
        const confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "text-action";
        confirm.textContent = "Confirm finding";
        confirm.addEventListener("click", () => confirmFinding(fact.fact_id, confirm));
        item.append(confirm);
      }
      const correction = document.createElement("button");
      correction.type = "button";
      correction.className = "text-action correction-unavailable";
      correction.disabled = true;
      correction.textContent = "Text correction unavailable";
      correction.title = "The current knowledge API supports confirmation only.";
      item.append(correction);
      facts.append(item);
    }
  }

  async function loadKnowledgeReport() {
    const state = $("#knowledge-state");
    if (!projectId) {
      if (state) state.textContent = "Project context required";
      return;
    }
    const requestedProjectId = projectId;
    const contextVersion = knowledgeContextVersion;
    const requestToken = ++knowledgeRequestToken;
    const isCurrentRequest = () => requestedProjectId === projectId
      && contextVersion === knowledgeContextVersion
      && requestToken === knowledgeRequestToken;
    if (state) state.textContent = "Loading project report…";
    try {
      const report = await request(`/api/projects/${encodeURIComponent(requestedProjectId)}/knowledge`);
      if (!isCurrentRequest()) return;
      renderKnowledgeReport(report);
      setActivity("Knowledge report loaded from the service.");
    } catch (error) {
      if (!isCurrentRequest()) return;
      knowledgeReport = null;
      clearRenderedKnowledgeReport();
      if (state) {
        state.textContent = error.code === "not_found"
          ? "Knowledge report unavailable — import source and wait for the worker"
          : reportError("Knowledge report unavailable", error);
      }
      setActivity(reportError("The service did not return a knowledge report", error));
    }
  }

  async function confirmFinding(factId, button) {
    if (!knowledgeReport || !projectId) return;
    const requestedProjectId = projectId;
    const contextVersion = knowledgeContextVersion;
    const reportAtRequest = knowledgeReport;
    button.disabled = true;
    setActivity("Confirming the finding with the service…");
    try {
      const report = await request(`/api/projects/${encodeURIComponent(requestedProjectId)}/knowledge/confirm`, {
        method: "POST",
        body: {
          expected_revision: knowledgeReport.revision,
          report_id: knowledgeReport.report_id,
          corrections: [{ fact_id: factId, status: "user_confirmed" }],
        },
      });
      if (requestedProjectId !== projectId || contextVersion !== knowledgeContextVersion || reportAtRequest !== knowledgeReport) return;
      renderKnowledgeReport(report);
      $("#knowledge-report-heading")?.focus();
      setActivity("Finding confirmed. The service returned a new knowledge report revision.");
    } catch (error) {
      button.disabled = false;
      setActivity(reportError("Finding was not confirmed", error));
    }
  }

  function selectedSourceKind() {
    return $("input[name=source-kind]:checked")?.value || "git";
  }

  function updateSourceFields() {
    const git = $("#git-source-fields");
    const zip = $("#zip-source-fields");
    const kind = selectedSourceKind();
    if (git) git.hidden = kind !== "git";
    if (zip) zip.hidden = kind !== "zip";
  }

  async function queueSourceImport() {
    const status = $("#source-import-status");
    if (!projectId) {
      if (status) status.textContent = "Project context required — use an existing project ID before importing.";
      return;
    }
    const requestedProjectId = projectId;
    const contextVersion = knowledgeContextVersion;
    const isCurrentContext = () => requestedProjectId === projectId
      && contextVersion === knowledgeContextVersion;
    const kind = selectedSourceKind();
    let source;
    try {
      if (kind === "zip") {
        const file = $("#source-zip-file")?.files?.[0];
        if (!file) throw new Error("Choose a ZIP archive before queueing the import");
        if (status) status.textContent = "Uploading ZIP archive…";
        const upload = await request("/api/uploads", {
          method: "POST",
          headers: { "Content-Type": file.type || "application/zip" },
          body: file,
        });
        source = { kind: "zip", upload_id: upload.upload_id };
      } else {
        const url = $("#source-git-url")?.value.trim();
        const ref = $("#source-git-ref")?.value.trim();
        if (!url || !ref) throw new Error("Provide both an HTTPS Git URL and an explicit ref");
        source = { kind: "git", url, ref };
      }
      if (!isCurrentContext()) return;
      if (status) status.textContent = "Queueing source import…";
      const queued = await request(`/api/projects/${encodeURIComponent(requestedProjectId)}/imports`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: source,
      });
      if (!isCurrentContext()) return;
      if (status) status.textContent = `Import ${queued.state}. Worker job ${queued.job_id} must finish before a report can load.`;
      const state = $("#knowledge-state");
      if (state) state.textContent = "Import queued — report pending";
      setActivity(`Source import ${queued.state}; no source claim has been made yet.`);
    } catch (error) {
      if (!isCurrentContext()) return;
      if (status) status.textContent = reportError("Source import was not queued", error);
      setActivity(reportError("Source import recovery needed", error));
    }
  }

  async function sendAuthoringNote() {
    const note = $("#authoring-note");
    const message = note?.value.trim();
    const session = window.__tracelineAuthoringSession;
    if (!session) {
      setActivity("Authoring note not sent — attach a saved session to use the session API.");
      return;
    }
    if (!message) {
      setActivity("Write an authoring note before sending it.");
      return;
    }
    setActivity("Sending authoring note…");
    try {
      const queued = await request(`/api/sessions/${encodeURIComponent(session.session_id)}/turns`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: { expected_revision: session.revision, message },
      });
      if (note) note.value = "";
      setActivity(`Authoring note ${queued.state}. Job ${queued.job_id} is awaiting the worker.`);
    } catch (error) {
      setActivity(reportError("Authoring note was not sent", error));
    }
  }

  async function resumeSession() {
    const binding = $("#authoring-binding-status");
    if (!sessionId) {
      setProjectContext(projectId);
      if (projectId) loadKnowledgeReport();
      return;
    }
    if (binding) binding.textContent = "Loading saved authoring session…";
    try {
      const session = await request(`/api/sessions/${encodeURIComponent(sessionId)}`);
      if (binding) binding.textContent = `Resumed session · revision ${session.revision}`;
      const saveState = $("#authoring-save-state");
      if (saveState) saveState.textContent = "Saved session restored";
      window.__tracelineAuthoringSession = session;
      setProjectContext(session.project_id || projectId);
      if (projectId) loadKnowledgeReport();
    } catch (error) {
      if (binding) binding.textContent = `Saved session unavailable: ${error.message}`;
      setActivity(reportError("Saved session unavailable", error));
      setProjectContext(projectId);
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

  function wireSourceAndActivity() {
    const projectInput = $("#authoring-project-id");
    if (projectInput) {
      projectInput.value = projectId;
      const handleProjectContextInput = () => {
        const nextProjectId = projectInput.value.trim();
        const session = window.__tracelineAuthoringSession;
        if (session?.project_id && session.project_id !== nextProjectId) {
          window.__tracelineAuthoringSession = null;
          const binding = $("#authoring-binding-status");
          if (binding) binding.textContent = nextProjectId
            ? `Session detached — project changed to ${nextProjectId}`
            : "Session detached — project context cleared";
          setActivity("Session actions detached. Attach a session for this project before sending authoring changes.");
        }
        if (nextProjectId !== projectId) clearKnowledgeReport();
        setProjectContext(nextProjectId);
      };
      projectInput.addEventListener("input", handleProjectContextInput);
      projectInput.addEventListener("change", handleProjectContextInput);
    }
    $("#load-project-report")?.addEventListener("click", loadKnowledgeReport);
    $("#refresh-knowledge-report")?.addEventListener("click", loadKnowledgeReport);
    all("input[name=source-kind]").forEach((input) => input.addEventListener("change", updateSourceFields));
    $("#queue-source-import")?.addEventListener("click", queueSourceImport);
    $("#send-authoring-note")?.addEventListener("click", sendAuthoringNote);
    updateSourceFields();
    setProjectContext(projectId);
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireTabs();
    wireMetrics();
    wireSourceAndActivity();
    all("[data-authoring-next]").forEach((button) => button.addEventListener("click", () => {
      const target = button.dataset.authoringNext;
      setTab(target === "dataset" ? "dataset" : target === "source" || target === "review" ? "reference" : target);
    }));
    resumeSession();
  });
}());
