/* Review-first authoring workspace. Persistent objects remain server-owned;
 * local storage holds only an unsaved browser draft so a failed request or
 * refresh cannot erase a person's selections. */
(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const params = new URLSearchParams(location.search);
  const sessionId = params.get("session_id");
  let evaluationId = (params.get("eval_id") || "").trim();
  let evaluationVersionId = (params.get("evaluation_version_id") || "").trim();
  let datasetVersionId = (params.get("dataset_version_id") || "").trim();
  let sourceVersionId = (params.get("source_version_id") || "").trim();
  let targetVersionId = (params.get("target_version_id") || "").trim();
  let dashboardVersionId = (params.get("dashboard_version_id") || "").trim();
  let targetConnectionState = {
    targetId: null, configuredVersionId: null, verificationJobId: null,
  };
  const tabs = ["reference", "metrics", "dataset", "preview"];
  let active = "reference";
  let projectId = (params.get("project_id") || "").trim();
  if (!targetVersionId) {
    try { targetVersionId = sessionStorage.getItem(`traceline.target-version:${sessionId || projectId || "none"}`) || ""; } catch (_) { /* storage unavailable */ }
  }
  let knowledgeReport = null;
  let knowledgeContextVersion = 0;
  let knowledgeRequestToken = 0;
  let draftStorageKey = null;
  const jobPollers = new Map();
  let pendingSessionMutation = false;
  const tabId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const revisionChannel = typeof BroadcastChannel === "function"
    ? new BroadcastChannel("traceline-authoring-revisions") : null;
  let conflictState = null;
  let datasetState = {
    datasetId: null, uploadId: null, reportId: null, report: null,
    versionId: null, revision: 0, excluded: new Set(), mapping: null,
  };
  let previewState = {
    definition: null, revision: 0, activeVersionId: null,
    previousDefinition: null, components: [], preview: null,
  };
  let runPlanState = {
    plan: null, planHash: null, authorization: null,
    requestedRefs: {}, idempotencyKey: null, pending: false,
  };

  const emptyDraft = () => ({ selected: [], customIntent: "", onMissing: "", onError: "", correctionDrafts: {} });
  let draft = emptyDraft();
  function draftKey() {
    const boundSessionId = window.__tracelineAuthoringSession?.session_id || sessionId || "";
    // A context-free page must not leave a draft that another project can inherit.
    if (!projectId && !boundSessionId) return null;
    return `traceline.authoring-draft.v2:project:${projectId || "none"}:session:${boundSessionId || "none"}`;
  }

  function loadDraft(key) {
    if (!key) return emptyDraft();
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "null");
      return saved && Array.isArray(saved.selected) ? { ...emptyDraft(), ...saved } : emptyDraft();
    } catch (_) {
      return emptyDraft();
    }
  }

  function saveDraft(message = "Draft saved in this browser") {
    if (draftStorageKey) {
      try { localStorage.setItem(draftStorageKey, JSON.stringify(draft)); } catch (_) { /* storage unavailable */ }
    }
    const status = $("#authoring-save-state");
    if (status) status.textContent = draftStorageKey ? message : "Choose a project or saved session to retain a browser draft";
  }

  function applyDraftToControls() {
    all(".metric-option input:not(:disabled)").forEach((input) => { input.checked = draft.selected.includes(input.value); });
    const intent = $("#authoring-custom-intent");
    const missing = $("#metric-on-missing");
    const error = $("#metric-on-error");
    if (intent) intent.value = draft.customIntent;
    if (missing) missing.value = draft.onMissing;
    if (error) error.value = draft.onError;
    updateSummary();
  }

  function activateDraftScope() {
    const nextKey = draftKey();
    if (nextKey === draftStorageKey && draft) return;
    draftStorageKey = nextKey;
    draft = loadDraft(nextKey);
    applyDraftToControls();
  }

  function clearActiveDraft() {
    if (draftStorageKey) {
      try { localStorage.removeItem(draftStorageKey); } catch (_) { /* storage unavailable */ }
    }
    draft = emptyDraft();
    applyDraftToControls();
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
    if (save) save.disabled = pendingSessionMutation || values.length === 0 || !draft.onMissing || !draft.onError;
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
      error.details = payload?.error?.details || {};
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function operationKey() {
    return `authoring-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function runVersionRefs() {
    const session = window.__tracelineAuthoringSession || {};
    const refs = {
      project_id: projectId || session.project_id || "",
      evaluation_version_id: evaluationVersionId || session.evaluation_version_id || "",
      dataset_version_id: datasetVersionId || session.dataset_version_id || "",
      source_version_id: sourceVersionId || session.source_version_id || "",
      target_version_id: targetVersionId || session.target_version_id || "",
      dashboard_version_id: dashboardVersionId || session.dashboard_version_id || "",
    };
    return Object.fromEntries(Object.entries(refs).filter(([, value]) => value));
  }

  const requiredRunPlanRefs = [
    ["project_id", "project_required", "Connect a project or provide project_id in the URL."],
    ["evaluation_version_id", "evaluation_version_required", "Provide a persisted evaluation version."],
    ["dataset_version_id", "dataset_version_required", "Commit a dataset version before planning a run."],
  ];

  function missingRunPlanBlockers(refs) {
    return requiredRunPlanRefs
      .filter(([key]) => !refs[key])
      .map(([, code, message]) => ({ code, message }));
  }

  function renderRunPlanRefs(refs) {
    const list = $("#run-plan-refs");
    if (!list) return;
    list.replaceChildren();
    const labels = {
      project_id: "Project",
      evaluation_version_id: "Evaluation version",
      dataset_version_id: "Dataset version",
      source_version_id: "Source version",
      target_version_id: "Target version",
      dashboard_version_id: "Dashboard version",
    };
    for (const [key, value] of Object.entries(refs || {})) {
      const row = document.createElement("div");
      appendText(row, "dt", labels[key] || key);
      appendText(row, "dd", value || "—", "mono");
      list.append(row);
    }
  }

  function renderRunPlanBlockers(blockers) {
    const list = $("#run-plan-blockers");
    if (!list) return;
    list.replaceChildren();
    for (const blocker of blockers || []) {
      const item = document.createElement("li");
      const code = typeof blocker === "string" ? blocker : blocker.code;
      const message = typeof blocker === "string" ? "The service returned this readiness blocker." : blocker.message;
      appendText(item, "code", code || "unknown_blocker");
      if (message) appendText(item, "span", ` — ${message}`);
      list.append(item);
    }
    const section = $("#run-plan-blockers-section");
    if (section) section.hidden = !blockers?.length;
  }

  function renderRunPlanResult(result, requestedRefs) {
    const plan = result?.plan || {};
    const refs = plan.content?.version_refs || plan.version_refs || requestedRefs || {};
    const blockers = plan.blockers || result?.blockers || [];
    const state = plan.state || result?.state || "unknown";
    const hash = plan.content_digest || plan.plan_hash || null;
    runPlanState.plan = plan;
    runPlanState.planHash = hash;
    runPlanState.authorization = null;
    runPlanState.requestedRefs = refs;
    const card = $("#run-plan-review-card");
    if (card) card.hidden = false;
    renderRunPlanRefs(refs);
    renderRunPlanBlockers(blockers);
    const stateLabel = $("#run-plan-state");
    if (stateLabel) stateLabel.textContent = state;
    const content = $("#run-plan-content");
    if (content) content.textContent = JSON.stringify(plan.content || plan, null, 2);
    const authorize = $("#authorize-run-plan");
    const enqueue = $("#enqueue-run-plan");
    if (authorize) authorize.hidden = state !== "validated" || !plan.plan_id || !hash || blockers.length > 0;
    if (enqueue) enqueue.hidden = true;
    const status = $("#run-plan-status");
    if (status) status.textContent = blockers.length
      ? "This manifest is blocked. Resolve every listed blocker before authorization."
      : state === "validated" ? "Manifest returned validated. Authorization is a separate explicit step." : `Observed plan state: ${state}.`;
  }

  async function reviewRunManifest() {
    const status = $("#run-plan-status");
    const button = $("#review-run-manifest");
    const refs = runVersionRefs();
    const missing = missingRunPlanBlockers(refs);
    runPlanState = { plan: null, planHash: null, authorization: null, requestedRefs: refs, idempotencyKey: null, pending: false };
    const card = $("#run-plan-review-card");
    if (card) card.hidden = false;
    renderRunPlanRefs(refs);
    if (missing.length) {
      renderRunPlanResult({ state: "blocked", plan: { state: "blocked", blockers: missing, content: { version_refs: refs } } }, refs);
      if (status) status.textContent = "Run planning is blocked before a request is sent.";
      return;
    }
    if (button) button.disabled = true;
    if (status) status.textContent = "Building the durable run manifest…";
    try {
      const result = await request("/api/run-plans", {
        method: "POST",
        body: { version_refs: refs, limits: {} },
      });
      renderRunPlanResult(result, refs);
      setActivity("The service returned a durable run manifest. Review blockers and authorize explicitly if it is ready.");
    } catch (error) {
      if (status) status.textContent = reportError("Run manifest was not created", error);
      setActivity(reportError("Run planning needs attention", error));
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function authorizeRunPlan() {
    const plan = runPlanState.plan;
    if (!plan?.plan_id || !runPlanState.planHash || runPlanState.pending) return;
    const button = $("#authorize-run-plan");
    const status = $("#run-plan-status");
    runPlanState.pending = true;
    if (button) button.disabled = true;
    if (status) status.textContent = "Requesting explicit authorization for this exact manifest…";
    try {
      const result = await request(`/api/run-plans/${encodeURIComponent(plan.plan_id)}/authorize`, {
        method: "POST",
        body: { plan_hash: runPlanState.planHash },
      });
      const authorization = result.authorization;
      if (!authorization?.authorization_id) throw new Error("The service did not return an authorization ID.");
      runPlanState.authorization = authorization;
      const enqueue = $("#enqueue-run-plan");
      if (enqueue) enqueue.hidden = false;
      if (status) status.textContent = `Authorization ${authorization.state || result.state || "returned"}. Queueing remains a separate action.`;
      setActivity("The service returned authorization for the reviewed manifest. Queue only after the explicit next step.");
    } catch (error) {
      if (status) status.textContent = reportError("Run manifest was not authorized", error);
      setActivity(reportError("Authorization needs attention", error));
      if (button) button.disabled = false;
    } finally {
      runPlanState.pending = false;
    }
  }

  async function enqueueRunPlan() {
    const plan = runPlanState.plan;
    const authorization = runPlanState.authorization;
    if (!plan?.plan_id || !runPlanState.planHash || !authorization?.authorization_id || runPlanState.pending) return;
    const button = $("#enqueue-run-plan");
    const status = $("#run-plan-status");
    runPlanState.pending = true;
    runPlanState.idempotencyKey ||= operationKey();
    if (button) button.disabled = true;
    if (status) status.textContent = "Queueing the authorized run…";
    try {
      const result = await request("/api/runs", {
        method: "POST",
        headers: { "Idempotency-Key": runPlanState.idempotencyKey },
        body: {
          plan_id: plan.plan_id,
          plan_hash: runPlanState.planHash,
          authorization_id: authorization.authorization_id,
        },
      });
      if (status) status.textContent = `Run ${result.state || "submitted"}. Worker job ${result.job_id || "returned by the service"}.`;
      setActivity(`The service returned run state ${result.state || "submitted"}; no measured result is claimed.`);
    } catch (error) {
      if (status) status.textContent = reportError("Authorized run was not queued", error);
      setActivity(reportError("Run queueing needs attention", error));
      if (button) button.disabled = false;
    } finally {
      runPlanState.pending = false;
    }
  }

  function reportError(prefix, error) {
    const correlation = error.correlationId ? ` (correlation ${error.correlationId})` : "";
    return `${prefix}: ${error.message}${correlation}`;
  }

  function setActivity(message) {
    const status = $("#authoring-activity-status");
    if (status) status.textContent = message;
  }

  function renderTargetErrors(errors) {
    const summary = $("#target-connection-errors");
    const list = $("#target-connection-error-list");
    if (!summary || !list) return;
    list.replaceChildren();
    for (const error of errors || []) appendText(list, "li", error);
    summary.hidden = !errors?.length;
  }

  function targetFormValues() {
    const maxTokensText = $("#target-max-tokens")?.value.trim() || "";
    return {
      url: $("#target-url")?.value.trim() || "",
      framework: $("#target-framework")?.value || "generic_http",
      mode: $("#target-mode")?.value || "stateless_json",
      model: $("#target-model")?.value.trim() || "",
      max_tokens: maxTokensText ? Number(maxTokensText) : null,
      authType: $("#target-auth-type")?.value || "none",
      secretRef: $("#target-secret-ref")?.value.trim() || "",
    };
  }

  function targetFormErrors(values) {
    const errors = [];
    try {
      const parsed = new URL(values.url);
      if (!["http:", "https:"].includes(parsed.protocol)) errors.push("URL must use HTTP or HTTPS.");
    } catch (_) {
      errors.push("Enter a valid HTTP or HTTPS target URL.");
    }
    if (values.authType !== "none" && !values.secretRef) {
      errors.push("A secret_ref is required for bearer or API-key authentication; inline secrets are not accepted.");
    }
    if (["anthropic_messages", "google_generative"].includes(values.framework) && !values.model) {
      errors.push("A model is required for this provider framework.");
    }
    if (["anthropic_messages", "google_generative"].includes(values.framework)
      && (!Number.isInteger(values.max_tokens) || values.max_tokens < 1 || values.max_tokens > 1_000_000)) {
      errors.push("max_tokens must be an integer between 1 and 1,000,000 for this provider framework.");
    }
    if (values.max_tokens != null && (!Number.isInteger(values.max_tokens) || values.max_tokens < 1 || values.max_tokens > 1_000_000)) {
      errors.push("max_tokens must be an integer between 1 and 1,000,000 when provided.");
    }
    if (!projectId) errors.push("Connect a project before configuring a hosted target.");
    return [...new Set(errors)];
  }

  function setTargetStatus(message, tone = "") {
    const status = $("#target-connection-status");
    if (status) {
      status.textContent = message;
      status.dataset.tone = tone;
    }
  }

  async function configureHostedTarget() {
    const values = targetFormValues();
    const errors = targetFormErrors(values);
    renderTargetErrors(errors);
    if (errors.length) {
      setTargetStatus("Target configuration is blocked. Resolve the listed fields.", "amber");
      $("#target-url")?.focus();
      return;
    }
    const button = $("#configure-target");
    if (button) button.disabled = true;
    setTargetStatus("Saving target configuration…");
    try {
      const body = {
        url: values.url,
        framework: values.framework,
        mode: values.mode,
        auth: values.authType === "none"
          ? { type: "none" }
          : { type: values.authType, secret_ref: values.secretRef },
      };
      if (values.model) body.model = values.model;
      if (values.max_tokens != null) body.max_tokens = values.max_tokens;
      const result = await request(`/api/projects/${encodeURIComponent(projectId)}/connections`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body,
      });
      if (!result.target_id || !result.version_id) throw new Error("The service did not return a target ID and configured version.");
      targetConnectionState = { targetId: result.target_id, configuredVersionId: result.version_id, verificationJobId: null };
      targetVersionId = "";
      const verify = $("#verify-target");
      if (verify) { verify.hidden = false; verify.disabled = false; }
      setTargetStatus(`Target configured (${result.state || "configured"}). Verification is required before it can be used in a run manifest.`);
      setActivity("Target configuration saved. No verification success is claimed yet.");
    } catch (error) {
      setTargetStatus(reportError("Target was not configured", error), "amber");
      setActivity(reportError("Target configuration needs attention", error));
    } finally {
      if (button) button.disabled = false;
    }
  }

  function persistVerifiedTarget(targetVersion) {
    targetVersionId = targetVersion;
    if (window.__tracelineAuthoringSession) window.__tracelineAuthoringSession.target_version_id = targetVersion;
    try { sessionStorage.setItem(`traceline.target-version:${sessionId || projectId || "none"}`, targetVersion); } catch (_) { /* storage unavailable */ }
    const next = new URL(location.href);
    next.searchParams.set("target_version_id", targetVersion);
    history.replaceState({}, "", next);
  }

  async function verifyHostedTarget() {
    const target = targetConnectionState;
    if (!target.targetId || !target.configuredVersionId) {
      setTargetStatus("Verify is blocked until the target configuration is saved.", "amber");
      return;
    }
    const button = $("#verify-target");
    if (button) button.disabled = true;
    setTargetStatus("Verification queued. Waiting for the durable worker result…");
    try {
      const queued = await request(`/api/targets/${encodeURIComponent(target.targetId)}/verify`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: {
          target_version_id: target.configuredVersionId,
          smoke_input: { message: "Traceline verification request" },
        },
      });
      if (!queued.job_id) throw new Error("The service did not return a verification job ID.");
      targetConnectionState.verificationJobId = queued.job_id;
      watchJob(queued.job_id, () => true, async (job) => {
        if (job.status === "completed") {
          const result = job.result || {};
          if (result.state === "verified" && result.target_version_id) {
            persistVerifiedTarget(result.target_version_id);
            setTargetStatus(`Target verified. Version ${result.target_version_id} is now available to Review run manifest.`, "green");
            setActivity("Verification completed with an observed verified target version.");
            if (button) button.disabled = true;
          } else {
            setTargetStatus("Verification completed without an observed verified target version.", "amber");
            setActivity("Verification did not return a verified target; the run manifest remains unbound to this target.");
            if (button) button.disabled = false;
          }
          return;
        }
        setTargetStatus(`Target verification ${job.status}: ${jobFailure(job)}`, "amber");
        setActivity(`Target verification ${job.status}: ${jobFailure(job)}`);
        if (button) button.disabled = false;
      }, "target_verification");
    } catch (error) {
      setTargetStatus(reportError("Target verification was not queued", error), "amber");
      setActivity(reportError("Target verification needs attention", error));
      if (button) button.disabled = false;
    }
  }

  function showConflict(kind, message, refresh) {
    conflictState = { kind, refresh };
    const card = $("#authoring-conflict-card");
    const title = $("#authoring-conflict-title");
    const detail = $("#authoring-conflict-detail");
    if (title) title.textContent = kind === "knowledge"
      ? "The knowledge report changed in another tab."
      : kind === "dataset" ? "The dataset changed in another tab." : "The dashboard changed in another tab.";
    if (detail) detail.textContent = message || "Reload the latest revision or keep this tab's unsaved draft for review.";
    if (card) card.hidden = false;
  }

  function hideConflict() {
    conflictState = null;
    const card = $("#authoring-conflict-card");
    if (card) card.hidden = true;
  }

  function broadcastRevision(kind, revision, versionId) {
    const message = { source: tabId, projectId, evaluationId, kind, revision, versionId };
    try { revisionChannel?.postMessage(message); } catch (_) { /* browser without channels */ }
    try { localStorage.setItem("traceline.authoring-revision", JSON.stringify(message)); } catch (_) { /* storage unavailable */ }
  }

  function hasUnsavedAuthoringDraft() {
    return selected().length > 0 || Boolean(draft.customIntent) || Boolean(datasetState.report)
      || (previewState.definition && previewState.activeVersionId && previewState.definition !== previewDefinition());
  }

  function handleRevisionMessage(message) {
    if (!message || message.source === tabId || message.projectId !== projectId) return;
    if (message.kind === "dashboard" && message.evaluationId !== evaluationId) return;
    const currentRevision = message.kind === "knowledge" ? (knowledgeReport?.revision || 0)
      : message.kind === "dataset" ? datasetState.revision : previewState.revision;
    if (Number(message.revision || 0) <= currentRevision) return;
    const refresh = message.kind === "knowledge" ? loadKnowledgeReport
      : message.kind === "dataset" ? refreshDatasetVersion : refreshDashboardVersions;
    showConflict(message.kind, `Revision ${message.revision} is now current on another tab.`, refresh);
  }

  function installRevisionListeners() {
    revisionChannel?.addEventListener("message", (event) => handleRevisionMessage(event.data));
    window.addEventListener("storage", (event) => {
      if (event.key !== "traceline.authoring-revision" || !event.newValue) return;
      try { handleRevisionMessage(JSON.parse(event.newValue)); } catch (_) { /* ignore malformed local state */ }
    });
    window.addEventListener("beforeunload", () => revisionChannel?.close());
  }

  function pendingJobsKey() {
    return projectId ? `traceline.authoring-jobs.v1:${projectId}:${sessionId || "none"}` : null;
  }

  function pendingJobs() {
    const key = pendingJobsKey();
    if (!key) return [];
    try {
      const value = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (_) { return []; }
  }

  function rememberJob(jobId, kind) {
    const key = pendingJobsKey();
    if (!key || !jobId) return;
    const jobs = pendingJobs().filter((item) => item.jobId !== jobId);
    jobs.push({ jobId, kind, projectId, evaluationId });
    try { localStorage.setItem(key, JSON.stringify(jobs)); } catch (_) { /* storage unavailable */ }
  }

  function forgetJob(jobId) {
    const key = pendingJobsKey();
    if (!key) return;
    const jobs = pendingJobs().filter((item) => item.jobId !== jobId);
    try { localStorage.setItem(key, JSON.stringify(jobs)); } catch (_) { /* storage unavailable */ }
  }

  function setSessionMutationPending(pending) {
    pendingSessionMutation = pending;
    const send = $("#send-authoring-note");
    if (send) send.disabled = pending;
    updateSummary();
  }

  function jobFailure(job) {
    const detail = job?.error?.message || job?.error?.detail || job?.error?.code;
    return detail ? String(detail) : "The worker did not complete the job.";
  }

  function watchJob(jobId, isCurrentContext, onTerminal, kind = "workflow") {
    if (!jobId || jobPollers.has(jobId)) return;
    const watcher = { stopped: false, timer: null, delay: 300 };
    jobPollers.set(jobId, watcher);
    const stop = () => {
      watcher.stopped = true;
      if (watcher.timer) clearTimeout(watcher.timer);
      jobPollers.delete(jobId);
    };
    const tick = async () => {
      if (watcher.stopped || !isCurrentContext()) return stop();
      try {
        const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (watcher.stopped || !isCurrentContext()) return stop();
        if (["completed", "failed", "cancelled"].includes(job.status)) {
          stop();
          forgetJob(jobId);
          await onTerminal(job);
          return;
        }
        watcher.delay = 500;
      } catch (_) {
        // A worker can be briefly unavailable during startup. Keep the local
        // draft and retry instead of presenting a queued change as completed.
        watcher.delay = Math.min(5000, watcher.delay * 1.7);
      }
      if (!watcher.stopped) watcher.timer = setTimeout(tick, watcher.delay);
    };
    rememberJob(jobId, kind);
    watcher.timer = setTimeout(tick, watcher.delay);
  }

  async function refreshBoundSession(sessionAtQueue) {
    const current = window.__tracelineAuthoringSession;
    if (!current || current.session_id !== sessionAtQueue.session_id || projectId !== sessionAtQueue.project_id) return null;
    const session = await request(`/api/sessions/${encodeURIComponent(sessionAtQueue.session_id)}`);
    if (!window.__tracelineAuthoringSession || window.__tracelineAuthoringSession.session_id !== sessionAtQueue.session_id
      || projectId !== session.project_id) return null;
    window.__tracelineAuthoringSession = session;
    activateDraftScope();
    const binding = $("#authoring-binding-status");
    if (binding) binding.textContent = `Resumed session · revision ${session.revision}`;
    return session;
  }

  function watchSessionJob(queued, sessionAtQueue, onCompleted) {
    watchJob(queued.job_id,
      () => window.__tracelineAuthoringSession?.session_id === sessionAtQueue.session_id && projectId === sessionAtQueue.project_id,
      async (job) => {
        if (job.status === "completed") {
          try {
            const fresh = await refreshBoundSession(sessionAtQueue);
            if (fresh) await onCompleted(fresh);
          } catch (error) {
            setActivity(reportError("The completed authoring change could not refresh its session", error));
          } finally {
            setSessionMutationPending(false);
          }
          return;
        }
        // Refresh after a conflict too: the server owns the current revision.
        if (job?.error?.code === "revision_conflict") {
          try { await refreshBoundSession(sessionAtQueue); } catch (_) { /* retain the actionable worker error below */ }
        }
        setActivity(`Authoring change ${job.status}: ${jobFailure(job)} Your local draft is retained.`);
        setSessionMutationPending(false);
      });
  }

  function setProjectContext(value) {
    projectId = value.trim();
    activateDraftScope();
    const input = $("#authoring-project-id");
    const load = $("#load-project-report");
    const importButton = $("#queue-source-import");
    const state = $("#knowledge-state");
    const bootstrap = $("#project-bootstrap-status");
    if (input && input.value !== projectId) input.value = projectId;
    if (load) load.disabled = !projectId;
    if (importButton) importButton.disabled = !projectId;
    if (!knowledgeReport && state) state.textContent = projectId ? "Source report not loaded" : "Project context required";
    const sourceStatus = $("#source-import-status");
    if (!projectId && sourceStatus) sourceStatus.textContent = "Choose a project before importing a source.";
    if (bootstrap) bootstrap.textContent = projectId
      ? `Project ${projectId} attached in this browser. Source and dataset objects remain server-owned.`
      : "No project attached.";
    const datasetButton = $("#validate-dataset");
    if (datasetButton) datasetButton.disabled = !projectId;
  }

  function connectProject() {
    const input = $("#authoring-project-id");
    const nextProjectId = input?.value.trim() || "";
    if (!nextProjectId) {
      $("#project-bootstrap-status").textContent = "Enter a project ID before connecting.";
      input?.focus();
      return;
    }
    if (nextProjectId !== projectId) clearKnowledgeReport();
    setProjectContext(nextProjectId);
    const next = new URL(location.href);
    next.searchParams.set("project_id", nextProjectId);
    history.replaceState({}, "", next);
    setActivity("Project context attached. Loading the server-owned report…");
    loadKnowledgeReport();
    refreshDatasetVersion();
    refreshDashboardVersions();
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
    sourceVersionId = report.source_version_id || sourceVersionId;
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
      correction.className = "text-action";
      correction.textContent = "Correct finding";
      correction.setAttribute("aria-expanded", "false");
      const editor = document.createElement("div");
      editor.className = "fact-correction-editor";
      editor.hidden = true;
      const label = appendText(editor, "label", "Correction", "visually-hidden");
      const textarea = document.createElement("textarea");
      textarea.rows = 3;
      textarea.className = "fact-correction-input";
      textarea.value = draft.correctionDrafts?.[fact.fact_id] || "";
      textarea.placeholder = "State the corrected understanding…";
      textarea.setAttribute("aria-label", `Correction for ${fact.name || "finding"}`);
      label.setAttribute("for", `correction-${fact.fact_id}`);
      textarea.id = `correction-${fact.fact_id}`;
      textarea.addEventListener("input", () => {
        draft.correctionDrafts = { ...(draft.correctionDrafts || {}), [fact.fact_id]: textarea.value };
        saveDraft("Correction retained in this browser");
      });
      const save = document.createElement("button");
      save.type = "button";
      save.className = "btn btn-sm btn-primary";
      save.textContent = "Save correction revision";
      save.addEventListener("click", () => saveFactCorrection(fact.fact_id, textarea, save));
      editor.append(textarea, save);
      correction.addEventListener("click", () => {
        editor.hidden = !editor.hidden;
        correction.setAttribute("aria-expanded", String(!editor.hidden));
        if (!editor.hidden) textarea.focus();
      });
      item.append(correction);
      item.append(editor);
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
      broadcastRevision("knowledge", report.revision, report.report_id);
      $("#knowledge-report-heading")?.focus();
      setActivity("Finding confirmed. The service returned a new knowledge report revision.");
    } catch (error) {
      button.disabled = false;
      if (error.code === "conflict") showConflict("knowledge", "The report revision is stale. Reload it before confirming.", loadKnowledgeReport);
      setActivity(reportError("Finding was not confirmed", error));
    }
  }

  async function saveFactCorrection(factId, textarea, button) {
    const correction = textarea?.value.trim();
    if (!knowledgeReport || !projectId || !correction) {
      setActivity("Write a correction before saving it.");
      textarea?.focus();
      return;
    }
    const reportAtRequest = knowledgeReport;
    button.disabled = true;
    setActivity("Saving the knowledge correction…");
    try {
      const created = await request(`/api/projects/${encodeURIComponent(projectId)}/knowledge/confirm`, {
        method: "POST",
        body: {
          expected_revision: reportAtRequest.revision,
          report_id: reportAtRequest.report_id,
          corrections: [{ fact_id: factId, status: "user_confirmed", summary: correction }],
        },
      });
      if (reportAtRequest !== knowledgeReport) return;
      await loadKnowledgeReport();
      broadcastRevision("knowledge", created.revision, created.report_id);
      setActivity("Correction saved. The service returned a new knowledge report revision.");
      draft.correctionDrafts = { ...(draft.correctionDrafts || {}) };
      delete draft.correctionDrafts[factId];
      saveDraft("Correction revision saved");
      $("#knowledge-report-heading")?.focus();
    } catch (error) {
      if (error.code === "conflict") showConflict("knowledge", "The report changed before this correction was saved.", loadKnowledgeReport);
      setActivity(reportError("Correction was not saved", error));
      button.disabled = false;
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
        source = { kind: "git", repo_url: url, ref };
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
      rememberJob(queued.job_id, "source_import");
      watchJob(queued.job_id, isCurrentContext, async (job) => {
        if (job.status === "completed") {
          if (status) status.textContent = "Import completed. Loading the new project report…";
          await loadKnowledgeReport();
          return;
        }
        if (status) status.textContent = `Import ${job.status}: ${jobFailure(job)}`;
        setActivity(`Source import ${job.status}: ${jobFailure(job)}`);
      }, "source_import");
    } catch (error) {
      if (!isCurrentContext()) return;
      if (status) status.textContent = reportError("Source import was not queued", error);
      setActivity(reportError("Source import recovery needed", error));
    }
  }

  function datasetFormat() {
    return $("#dataset-format")?.value || "jsonl";
  }

  function datasetMapping() {
    const format = datasetFormat();
    const caseId = $("#dataset-case-id")?.value.trim();
    const input = $("#dataset-input-field")?.value.trim();
    const expected = $("#dataset-expected-field")?.value.trim();
    const label = $("#dataset-label-status")?.value.trim();
    if (!caseId || !input || !expected) throw new Error("Case ID, input and expected mappings are required");
    if (format === "csv") {
      return {
        format, case_id: caseId,
        input_fields: { query: input },
        expected_fields: { answer: expected },
        label_status: label || null,
      };
    }
    return { format, case_id: caseId, input, expected, label_status: label || null };
  }

  function updateDatasetFormat() {
    const hint = $("#dataset-csv-hint");
    if (hint) hint.hidden = datasetFormat() !== "csv";
    const file = $("#dataset-file")?.files?.[0];
    if (!file) return;
    const name = file.name.toLowerCase();
    if (name.endsWith(".csv")) $("#dataset-format").value = "csv";
    else if (name.endsWith(".json")) $("#dataset-format").value = "json";
    else if (name.endsWith(".jsonl")) $("#dataset-format").value = "jsonl";
    if (hint) hint.hidden = datasetFormat() !== "csv";
  }

  function labelTone(status) {
    return status === "approved" ? "approved" : status === "inferred" ? "inferred" : "missing";
  }

  function renderDatasetReport(report) {
    const card = $("#dataset-report-card");
    const rows = $("#dataset-report-rows");
    if (!card || !rows || !report) return;
    card.hidden = false;
    const coverage = report.label_coverage || {};
    $("#dataset-report-meta").textContent = `${report.cases?.length || 0} cases · ${coverage.approved || 0} approved labels · ${coverage.missing || 0} missing`;
    $("#dataset-coverage").textContent = `${coverage.approved || 0}/${coverage.total || 0} approved`;
    $("#dataset-report-callout").textContent = "Review each row before committing. Exclusions are explicit and create a new immutable dataset revision; missing labels are never promoted to gold automatically.";
    rows.replaceChildren();
    for (const item of report.cases || []) {
      const tr = document.createElement("tr");
      const keepCell = document.createElement("td");
      const keep = document.createElement("input");
      keep.type = "checkbox";
      keep.checked = !datasetState.excluded.has(item.case_id);
      keep.setAttribute("aria-label", `Keep case ${item.case_id}`);
      keep.addEventListener("change", () => {
        if (keep.checked) datasetState.excluded.delete(item.case_id);
        else datasetState.excluded.add(item.case_id);
        saveDraft("Dataset exclusions retained in this browser");
      });
      keepCell.append(keep);
      const caseCell = document.createElement("td");
      appendText(caseCell, "strong", item.case_id || "—");
      if (item.metadata?.row_number || item.row_number) appendText(caseCell, "small", `row ${item.metadata?.row_number || item.row_number}`);
      const inputCell = document.createElement("td");
      inputCell.textContent = JSON.stringify(item.input ?? null);
      const expectedCell = document.createElement("td");
      expectedCell.textContent = JSON.stringify(item.expected ?? null);
      const labelCell = document.createElement("td");
      const badge = appendText(labelCell, "span", item.label_status || "missing", `dataset-label ${labelTone(item.label_status)}`);
      badge.title = item.label_provenance ? `Provenance: ${item.label_provenance}` : "No independent label provenance";
      tr.append(keepCell, caseCell, inputCell, expectedCell, labelCell);
      rows.append(tr);
    }
  }

  async function validateDataset() {
    const status = $("#dataset-import-status");
    if (!projectId) {
      if (status) status.textContent = "Connect a project before importing cases.";
      return;
    }
    const file = $("#dataset-file")?.files?.[0];
    if (!file) {
      if (status) status.textContent = "Choose a CSV, JSON or JSONL file first.";
      return;
    }
    try {
      const mapping = datasetMapping();
      if (status) status.textContent = "Uploading dataset…";
      const upload = await request("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      if (status) status.textContent = "Validating mapping and labels…";
      const report = await request(`/api/projects/${encodeURIComponent(projectId)}/datasets/imports`, {
        method: "POST",
        body: {
          upload_id: upload.upload_id,
          dataset_id: datasetState.datasetId || null,
          mapping,
          metric_requirements: [
            { metric_id: "latency", class: "health", requires_label: false },
            { metric_id: "accuracy", class: "accuracy", requires_label: true },
          ],
        },
      });
      datasetState = {
        datasetId: report.dataset_id, uploadId: upload.upload_id, reportId: report.report_id,
        report, versionId: datasetState.versionId, revision: datasetState.revision,
        excluded: new Set(), mapping,
      };
      renderDatasetReport(report);
      if (status) status.textContent = "Mapping validated. Nothing is committed until you choose the rows to keep.";
      setActivity("Dataset mapping validated by the service; review the label provenance before committing.");
      const next = new URL(location.href);
      next.searchParams.set("dataset_id", report.dataset_id);
      history.replaceState({}, "", next);
    } catch (error) {
      if (status) status.textContent = reportError("Dataset was not validated", error);
      setActivity(reportError("Dataset mapping needs attention", error));
    }
  }

  async function refreshDatasetVersion() {
    if (!datasetState.datasetId && !params.get("dataset_id")) return;
    const id = datasetState.datasetId || params.get("dataset_id");
    try {
      const listing = await request(`/api/objects/dataset/${encodeURIComponent(id)}/versions`);
      datasetState.datasetId = id;
      datasetState.revision = Number(listing.active_revision || 0);
      datasetState.versionId = listing.active_version_id || null;
      if (datasetState.versionId) {
        const version = await request(`/api/datasets/${encodeURIComponent(id)}/versions/${encodeURIComponent(datasetState.versionId)}`);
        datasetState.report = version;
        renderDatasetReport(version);
      }
      datasetVersionId = datasetState.versionId || datasetVersionId;
      updateDatasetRevisionLabel();
    } catch (error) {
      if (error.code !== "not_found") setActivity(reportError("Dataset version could not be refreshed", error));
    }
  }

  function updateDatasetRevisionLabel() {
    const label = $("#dataset-revision");
    if (label) label.textContent = datasetState.versionId ? `Revision ${datasetState.revision}` : "No dataset version";
  }

  async function commitDataset() {
    const status = $("#dataset-commit-status");
    if (!datasetState.datasetId || !datasetState.reportId) {
      if (status) status.textContent = "Validate a mapping before committing a dataset.";
      return;
    }
    try {
      const listing = await request(`/api/objects/dataset/${encodeURIComponent(datasetState.datasetId)}/versions`);
      const expectedRevision = Number(listing.active_revision || 0);
      const result = await request(`/api/datasets/${encodeURIComponent(datasetState.datasetId)}/commit`, {
        method: "POST",
        body: {
          report_id: datasetState.reportId,
          explicit_exclusions: [...datasetState.excluded],
          expected_revision: expectedRevision,
        },
      });
      datasetState.revision = result.revision;
      datasetState.versionId = result.version_id;
      datasetVersionId = result.version_id;
      updateDatasetRevisionLabel();
      const next = new URL(location.href);
      next.searchParams.set("dataset_version_id", result.version_id);
      history.replaceState({}, "", next);
      broadcastRevision("dataset", result.revision, result.version_id);
      if (status) status.textContent = `Dataset revision ${result.revision} committed. ${datasetState.excluded.size} explicit exclusion(s).`;
      setActivity("Dataset committed. The service returned a new immutable version.");
      renderDatasetReport({ ...datasetState.report, exclusions: [...datasetState.excluded].map((case_id) => ({ case_id, reason: "explicit" })) });
    } catch (error) {
      if (error.code === "conflict") showConflict("dataset", "A newer dataset revision exists; reload it before committing.", refreshDatasetVersion);
      if (status) status.textContent = reportError("Dataset was not committed", error);
    }
  }

  function clearDataset() {
    datasetState = { datasetId: null, uploadId: null, reportId: null, report: null, versionId: null, revision: 0, excluded: new Set(), mapping: null };
    if ($("#dataset-report-card")) $("#dataset-report-card").hidden = true;
    if ($("#dataset-file")) $("#dataset-file").value = "";
    if ($("#dataset-import-status")) $("#dataset-import-status").textContent = projectId ? "Choose a dataset file to begin." : "Choose a project and a dataset file to begin.";
    updateDatasetRevisionLabel();
  }

  function previewDefinition() {
    const metricIds = selected();
    const registryVersion = Number($("#preview-registry-version")?.value || 1);
    return {
      version: 3,
      name: $("#preview-name")?.value.trim() || "Evaluation overview",
      registry_version: registryVersion,
      layout: { type: "grid", columns: 12 },
      filters: [{ id: "run", type: "run_selector", default: "latest" }],
      blocks: metricIds.map((metricId, index) => ({
        component: "metric_summary", span: index % 2 === 0 ? 6 : 6,
        bind: { metric: metricId, run: "$filters.run", stat: metricId === "latency" ? "mean" : "pass_rate" },
      })),
    };
  }

  function renderPreviewEditor() {
    const editor = $("#preview-block-editor");
    if (!editor) return;
    editor.replaceChildren();
    const metricIds = selected();
    if (!metricIds.length) {
      appendText(editor, "p", "Select at least one metric to add a registered summary block.", "preview-editor-empty");
      return;
    }
    for (const metricId of metricIds) {
      const row = document.createElement("div");
      row.className = "preview-block-row";
      appendText(row, "span", metricId, "mono");
      appendText(row, "span", metricId === "latency" ? "mean" : "pass_rate", "preview-block-binding");
      editor.append(row);
    }
  }

  function renderPreviewResult(preview) {
    const card = $("#preview-result-card");
    const blocks = $("#preview-result-blocks");
    if (!card || !blocks || !preview) return;
    card.hidden = false;
    $("#preview-result-meta").textContent = `Generator ${preview.generator_version || "?"} · data hash ${String(preview.data_hash || "").slice(0, 12)}… · no gate result`;
    blocks.replaceChildren();
    for (const block of preview.blocks || []) {
      const item = document.createElement("article");
      item.className = "preview-result-block";
      appendText(item, "h4", block.metric_id || block.component || "registered component");
      appendText(item, "strong", block.placeholder ? "Placeholder" : String(block.sample), "preview-result-value");
      appendText(item, "p", block.placeholder ? (block.reason || "No declared range") : "Deterministic synthetic example; not a measured result.");
      blocks.append(item);
    }
  }

  function renderPreviewDefinitionInspector() {
    const inspector = $("#preview-definition-inspector");
    if (inspector && !inspector.hidden) inspector.textContent = JSON.stringify(previewDefinition(), null, 2);
  }

  async function refreshDashboardVersions() {
    if (!evaluationId && window.__tracelineAuthoringSession?.evaluation_id) evaluationId = window.__tracelineAuthoringSession.evaluation_id;
    if (!evaluationId) return;
    try {
      const listing = await request(`/api/objects/dashboard/${encodeURIComponent(evaluationId)}/versions`);
      const activeVersion = listing.versions?.find((item) => item.version_id === listing.active_version_id);
      previewState.revision = Number(listing.active_revision || 0);
      previewState.activeVersionId = listing.active_version_id || null;
      dashboardVersionId = previewState.activeVersionId || dashboardVersionId;
      if (activeVersion) {
        previewState.definition = activeVersion.content?.definition || activeVersion.content;
        previewState.previousDefinition = listing.versions?.[listing.versions.length - 2]?.content?.definition || null;
        $("#preview-name").value = previewState.definition.name || "Evaluation overview";
      } else {
        previewState.definition = previewDefinition();
      }
      renderPreviewEditor();
      updatePreviewRevisionLabel();
    } catch (error) {
      if (error.code !== "not_found") setActivity(reportError("Dashboard revisions could not be refreshed", error));
    }
  }

  function updatePreviewRevisionLabel() {
    const label = $("#preview-revision");
    if (label) label.textContent = previewState.activeVersionId ? `Revision ${previewState.revision}` : "No revision saved";
    const undo = $("#undo-preview-definition");
    if (undo) undo.disabled = !previewState.previousDefinition || !previewState.activeVersionId;
  }

  async function queuePreviewJob(versionId) {
    const status = $("#preview-save-status");
    if (!evaluationVersionId || !datasetVersionId) {
      if (status) status.textContent = "Dashboard revision saved. Preview generation needs an evaluation version and committed dataset version.";
      return;
    }
    try {
      const queued = await request(`/api/dashboard-versions/${encodeURIComponent(versionId)}/preview`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: { evaluation_version_id: evaluationVersionId, dataset_version_id: datasetVersionId, generator_version: "1" },
      });
      if (status) status.textContent = `Preview ${queued.state}. Worker job ${queued.job_id} is awaiting completion.`;
      watchJob(queued.job_id, () => true, async (job) => {
        if (job.status === "completed") {
          previewState.preview = job.result || {};
          renderPreviewResult(previewState.preview);
          if (status) status.textContent = "Synthetic preview generated from the saved canonical definition.";
          setActivity("Preview worker completed; no evaluation was started.");
        } else if (status) status.textContent = `Preview ${job.status}: ${jobFailure(job)}`;
      }, "dashboard_preview");
    } catch (error) {
      if (status) status.textContent = reportError("Preview was not queued", error);
    }
  }

  async function savePreviewDefinition(definition = previewDefinition(), isUndo = false) {
    const status = $("#preview-save-status");
    if (!evaluationId) {
      if (status) status.textContent = "Connect a saved evaluation/session before saving a dashboard revision.";
      return;
    }
    if (!definition.blocks.length) {
      if (status) status.textContent = "Select at least one metric before saving the dashboard.";
      return;
    }
    const before = previewState.definition;
    try {
      if (status) status.textContent = "Saving dashboard revision…";
      const result = await request(`/api/dashboards/${encodeURIComponent(evaluationId)}/versions`, {
        method: "POST",
        body: { expected_revision: previewState.revision, definition },
      });
      const version = result.version;
      previewState.previousDefinition = before;
      previewState.definition = definition;
      previewState.revision = version.revision;
      previewState.activeVersionId = version.version_id;
      updatePreviewRevisionLabel();
      broadcastRevision("dashboard", version.revision, version.version_id);
      if (status) status.textContent = isUndo
        ? `Undo saved as dashboard revision ${version.revision}.`
        : `Dashboard revision ${version.revision} saved and validated.`;
      setActivity("Dashboard definition saved by the service; presentation is still separate from run approval.");
      await queuePreviewJob(version.version_id);
    } catch (error) {
      if (error.code === "conflict") showConflict("dashboard", "A newer dashboard revision exists; reload it before saving.", refreshDashboardVersions);
      if (status) status.textContent = reportError("Dashboard revision was not saved", error);
    }
  }

  function wireDataset() {
    $("#dataset-file")?.addEventListener("change", updateDatasetFormat);
    $("#dataset-format")?.addEventListener("change", updateDatasetFormat);
    $("#validate-dataset")?.addEventListener("click", validateDataset);
    $("#commit-dataset")?.addEventListener("click", commitDataset);
    $("#clear-dataset")?.addEventListener("click", clearDataset);
  }

  function wirePreview() {
    $("#preview-name")?.addEventListener("input", renderPreviewDefinitionInspector);
    $("#save-preview-definition")?.addEventListener("click", () => savePreviewDefinition());
    $("#undo-preview-definition")?.addEventListener("click", () => savePreviewDefinition(previewState.previousDefinition, true));
    $("#inspect-preview-definition")?.addEventListener("click", (event) => {
      const inspector = $("#preview-definition-inspector");
      if (!inspector) return;
      inspector.hidden = !inspector.hidden;
      inspector.textContent = JSON.stringify(previewDefinition(), null, 2);
      event.currentTarget.setAttribute("aria-expanded", String(!inspector.hidden));
      event.currentTarget.textContent = inspector.hidden ? "Inspect JSON" : "Hide JSON";
    });
    $("#review-run-manifest")?.addEventListener("click", reviewRunManifest);
    $("#authorize-run-plan")?.addEventListener("click", authorizeRunPlan);
    $("#enqueue-run-plan")?.addEventListener("click", enqueueRunPlan);
    all(".metric-option input:not(:disabled)").forEach((input) => input.addEventListener("change", renderPreviewEditor));
    renderPreviewEditor();
  }

  async function reconcilePendingJobs() {
    for (const pending of pendingJobs()) {
      if (pending.projectId !== projectId) continue;
      if (pending.kind === "source_import") {
        const requestedProjectId = projectId;
        watchJob(pending.jobId, () => requestedProjectId === projectId, async (job) => {
          const status = $("#source-import-status");
          if (job.status === "completed") {
            if (status) status.textContent = "Import completed while this page was away. Loading the project report…";
            await loadKnowledgeReport();
          } else {
            if (status) status.textContent = `Import ${job.status}: ${jobFailure(job)}`;
            setActivity(`Source import ${job.status}: ${jobFailure(job)}`);
          }
        }, "source_import");
      } else if (pending.kind === "session_turn" && window.__tracelineAuthoringSession) {
        const sessionAtQueue = window.__tracelineAuthoringSession;
        watchJob(pending.jobId,
          () => window.__tracelineAuthoringSession?.session_id === sessionAtQueue.session_id && projectId === sessionAtQueue.project_id,
          async (job) => {
            if (job.status === "completed") {
              try {
                await refreshBoundSession(sessionAtQueue);
                setActivity("Authoring worker completed while this page was away; the session was reconciled from the service.");
              } finally { setSessionMutationPending(false); }
            } else {
              setActivity(`Authoring change ${job.status}: ${jobFailure(job)} Your local draft is retained.`);
              setSessionMutationPending(false);
            }
          }, "session_turn");
      } else if (pending.kind === "dashboard_preview") {
        watchJob(pending.jobId, () => true, async (job) => {
          if (job.status === "completed") {
            previewState.preview = job.result || {};
            renderPreviewResult(previewState.preview);
            $("#preview-save-status").textContent = "Synthetic preview reconciled from the completed worker job.";
          } else {
            $("#preview-save-status").textContent = `Preview ${job.status}: ${jobFailure(job)}`;
          }
        }, "dashboard_preview");
      }
    }
  }

  async function reloadConflict() {
    const current = conflictState;
    hideConflict();
    if (!current?.refresh) return;
    try {
      await current.refresh();
      setActivity("Latest revision loaded. Review any local draft before saving again.");
    } catch (error) {
      setActivity(reportError("The latest revision could not be loaded", error));
    }
  }

  function keepConflictDraft() {
    hideConflict();
    saveDraft("Local draft kept after a cross-tab conflict");
    setActivity("Your local draft is kept. Reload the latest revision manually before committing it.");
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
    if (pendingSessionMutation) {
      setActivity("An authoring change is still awaiting the worker. Wait for the session revision to refresh.");
      return;
    }
    setActivity("Sending authoring note…");
    setSessionMutationPending(true);
    try {
      const queued = await request(`/api/sessions/${encodeURIComponent(session.session_id)}/turns`, {
        method: "POST",
        headers: { "Idempotency-Key": operationKey() },
        body: { expected_revision: session.revision, message },
      });
      setActivity(`Authoring note ${queued.state}. Job ${queued.job_id} is awaiting the worker.`);
      watchSessionJob(queued, session, async (fresh) => {
        if (note) note.value = "";
        setActivity(`Authoring note completed. Session revision ${fresh.revision} is now current.`);
      });
    } catch (error) {
      setSessionMutationPending(false);
      setActivity(reportError("Authoring note was not sent", error));
    }
  }

  async function resumeSession() {
    const binding = $("#authoring-binding-status");
    if (!sessionId) {
      setProjectContext(projectId);
      if (projectId) {
        loadKnowledgeReport();
        refreshDatasetVersion();
        refreshDashboardVersions();
        reconcilePendingJobs();
      }
      return;
    }
    if (binding) binding.textContent = "Loading saved authoring session…";
    try {
      const session = await request(`/api/sessions/${encodeURIComponent(sessionId)}`);
      if (binding) binding.textContent = `Resumed session · revision ${session.revision}`;
      const saveState = $("#authoring-save-state");
      if (saveState) saveState.textContent = "Saved session restored";
      window.__tracelineAuthoringSession = session;
      evaluationId = session.evaluation_id || evaluationId;
      evaluationVersionId = session.evaluation_version_id || evaluationVersionId;
      datasetVersionId = session.dataset_version_id || datasetVersionId;
      sourceVersionId = session.source_version_id || sourceVersionId;
      targetVersionId = session.target_version_id || targetVersionId;
      dashboardVersionId = session.dashboard_version_id || dashboardVersionId;
      setProjectContext(session.project_id || projectId);
      if (projectId) {
        loadKnowledgeReport();
        refreshDatasetVersion();
        refreshDashboardVersions();
        reconcilePendingJobs();
      }
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
    if (pendingSessionMutation) {
      if (help) help.textContent = "An authoring change is still awaiting the worker. Your local draft is retained.";
      return;
    }
    const button = $("#save-metric-decisions");
    if (button) button.disabled = true;
    if (help) help.textContent = "Saving the reviewed proposal…";
    setSessionMutationPending(true);
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
      watchSessionJob(queued, session, async (fresh) => {
        clearActiveDraft();
        if (help) help.textContent = `Proposal completed. Session revision ${fresh.revision} is now current.`;
      });
    } catch (error) {
      setSessionMutationPending(false);
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
    $("#connect-project")?.addEventListener("click", connectProject);
    $("#load-project-report")?.addEventListener("click", loadKnowledgeReport);
    $("#refresh-knowledge-report")?.addEventListener("click", loadKnowledgeReport);
    all("input[name=source-kind]").forEach((input) => input.addEventListener("change", updateSourceFields));
    $("#queue-source-import")?.addEventListener("click", queueSourceImport);
    $("#send-authoring-note")?.addEventListener("click", sendAuthoringNote);
    $("#configure-target")?.addEventListener("click", configureHostedTarget);
    $("#verify-target")?.addEventListener("click", verifyHostedTarget);
    $("#reload-conflict")?.addEventListener("click", reloadConflict);
    $("#keep-conflict-draft")?.addEventListener("click", keepConflictDraft);
    updateSourceFields();
    setProjectContext(projectId);
  }

  document.addEventListener("DOMContentLoaded", () => {
    installRevisionListeners();
    wireTabs();
    wireMetrics();
    wireSourceAndActivity();
    wireDataset();
    wirePreview();
    all("[data-authoring-next]").forEach((button) => button.addEventListener("click", () => {
      const target = button.dataset.authoringNext;
      setTab(target === "dataset" ? "dataset" : target === "source" || target === "review" ? "reference" : target);
    }));
    resumeSession();
  });
}());
