# Workflow UI and UX plan

Date: 2026-09-08. Status: implementation specification; the target behavior below is not a claim of shipped functionality.

This is the interface companion to [the end-to-end workflow plan](end-to-end-workflow-plan.md) and [the implementation plan](workflow-implementation-plan.md). Read their execution, persistence, and security contracts together with this document. [v3](LLM_Agent_Evaluation_Engine_v3.md) remains the underlying evaluation specification; this plan extends it for the newly requested hosted-agent connection and turns its UX requirements into reviewable implementation criteria.

## 1. Product promise and release boundary

A person connects an agent, reviews what the platform understands, chooses what good behavior means, previews the resulting dashboard, supplies representative tests, verifies an execution target, and runs an evaluation. They can trace any result to its evidence, compare a changed agent, and export a reproducible report. Chat is an accessible control surface for this work; persistent project objects own its state.

**R1:** complete this loop with local isolated execution and hosted synchronous stateless JSON HTTP execution, source import by Git or ZIP, optional API-only onboarding, structured metric selection, deterministic dashboard previews, dataset import and labels, progressive results, regression comparisons, evidence, re-scoring, and exports. Existing accepted scope cuts remain in force. Ordinary run regression comparison is core; a dedicated pairwise preference judging board, dashboard plugins, production replay product workflow, dedicated CI configuration UI, and human review queues remain deferred.

**R2:** hosted streaming, asynchronous jobs and isolated remote sessions; scripted multi-turn/approval interactions, specialized retrieval metrics and recurring synthetic runs. Do not show an R2 action as enabled in R1, even if isolated engine building blocks already exist.

Nontechnical users can author, inspect, and compare through plain language and forms. They may still need an agent owner to supply a request contract, credentials, or labels. The UI must identify that dependency without asking them to write frontend code or evaluator code.

## 2. Current implementation alignment audit

The repository now contains application code and a static web client. The old design-only statement in `AGENTS.md` does not describe this checkout. The following findings come from source inspection on the date above. Line references are anchors into that checkout, not proof of runtime behavior.

| User need | Current evidence | Required completion |
|---|---|---|
| Chat-first onboarding | `web/index.html:84` has a harness conversation; `web/app.js:2438` sends messages to `/api/harness/chat`. | Persist sessions, typed messages and proposal objects; resume the exact decision after reload. |
| Git or ZIP source | ZIP input at `web/index.html:121`; upload at `web/app.js:2477`; extraction endpoint at `src/llm_agent_eval/api.py:1265`. Current help offers ZIP/local path. | Add Git import with resolved commit, verified source identity, durable upload records, progress and cancellation. Never silently substitute an example for an invalid source. |
| Accurate understanding | `web/app.js:2334` renders tools, entrypoint and files. `/api/projects/analyze` uses canned fallback tools/models at `api.py:1202` and returns `smoke_passed: True` at `api.py:1261` without a smoke invocation in that handler. | Replace inferred readiness with measured evidence. Store a versioned reference report with user corrections, provenance and unresolved questions. |
| Explicit confirmation | “What we found” is a display card; `web/app.js:2337` treats every value except `false`, including absent smoke evidence, as `SMOKE READY`. | Distinct unknown, checking, verified and failed states. Bind acceptance to the report revision; do not infer acceptance from a later chat sentence. |
| Metric suggestions | `web/app.js:2455` renders suggestion chips; `web/app.js:2495` sends their text immediately. | Typed metric options with stable IDs, multi-selection, inspect/edit, availability reason and explicit apply. Suggestions are proposals, not commits. |
| Editable dashboard preview | Separate placeholder path at `web/app.js:2103`; it recognizes three components and omits unsupported ones. `orchestrator.py:318` returns `dashboard_name`, blocks with row/col, and hardcoded KPI gates. | One canonical validated definition and renderer for preview and results. Revisions and review status; unknown components must surface a validation error, never disappear. |
| Declarative architecture | `dashboard.py:139` defines versioned layout/filters/blocks; `dashboard.py:61` has a closed binding grammar. `web/app.js:895` renders registered blocks. | Reuse and extend this contract instead of introducing another template language. Unify authored layout with the canonical definition. |
| Preview becomes real results | `web/app.js:2223` selects a saved evaluation, but a run then loads the default dashboard through `loadDashboard()`/`dashboardUrl()` at `web/app.js:270`. | Resolve the evaluation's pinned definition against its pinned run. Preserve its blocks and filters when switching from synthetic to recorded data. |
| Conversational revision memory | `api.py:1339` creates an orchestrator per request; `api.py:1340` passes the message/project ID, while `eval_id` is used later for saving. Browser messages are appended to DOM at `web/app.js:2399`. | Retrieve session and current artifact revisions before authoring. Typed operations replace uncontrolled overwrites; conflicts are visible. |
| Hosted agent | Current custom execution requires `entrypoint` and passes local `cwd` in `api.py:1304`. | A target selector and hosted connector contract, with evidence capability disclosure and request verification. |
| Run review | `web/app.js:2247` immediately posts `/api/evals/{id}/run`; `api.py:1321` starts that run. | Review a concrete execution manifest and enforce its approval server-side before queueing. |
| Results/evidence | SSE at `web/app.js:1700`; case inspector/trace timeline at `web/app.js:1464`/`:1534`; evidence endpoint at `api.py:882`. | Retain useful foundations; add capability-aware labels, immutable links, reconnect reconciliation and consistent partial/final states. |
| Disputes | Existing score revision UI at `web/app.js:1759`; request at `:1794`; endpoint at `api.py:902`. | Preserve original machine result, scope the dispute to a revision, distinguish override from re-scoring, and show audit provenance. |
| Datasets, reports and comparisons | Case input/expected inspection exists at `web/app.js:1668`. No end-to-end dataset mapping, report export, or dedicated regression comparison flow appears in the inspected web client. | Add the specified surfaces without implying that viewing cases is dataset authoring or that listing runs is comparison. |
| Accessibility and interaction | Skip link at `web/index.html:13`; preference media rules at `web/styles.css:742`; artifact spring at `web/app.js:2062`. Several actions use only `pointerdown`, e.g. `web/app.js:2215` and `:2495–2539`. | Retain foundations. Pointer-down gives visual feedback; release/native activation commits. Keyboard activation must perform the same action. Audit focus, scrolling, screen-reader announcements and all preference combinations. |

Verification limitation: an isolated launch using the existing `.venv` and a temporary database failed during `create_app`, because the ZIP upload route requires `python-multipart`, which was not installed in that environment. No dependencies or UI source were changed for this documentation audit. The live UI, layout, motion and end-to-end behavior were therefore **not visually verified**. Source findings above must not be described as successful browser tests.

## 3. Information architecture and object ownership

Use a project selector and stable links. Within a project expose **Overview, Agent reference, Evaluations, Datasets, Runs, Dashboards, Connections**. A workflow selector appears when an agent contains multiple evaluable workflows. An evaluation has **Plan, Chat, Dataset, Preview, Runs**. An individual run has **Summary, Cases, Evidence, Compare, Export**. Do not force a novice to visit every page: chat cards open the relevant artifact in a companion panel.

On desktop the evaluation workspace has the conversation and one active artifact beside it. A breadcrumb identifies project/workflow/evaluation. The artifact header shows name, revision, save state and provenance; tabs switch between reference, metrics, dataset and dashboard without losing the conversation. Advanced JSON is a secondary “Inspect definition” view using the same validated object.

| Object | Visible ownership and persistence |
|---|---|
| Source snapshot | Git commit/archive digest, imported timestamp, parse status; changing source creates a new snapshot. |
| Agent reference | Confirmed report revision, source references, uncertainties, declared vs observed capabilities. |
| Harness session | Session ID and typed messages referring to persisted objects; reload reconstructs pending work from those objects. |
| EvaluationSpec revision | Metric selection, scoring, aggregation, gates, dataset binding, validation and authorship. |
| Dataset revision | Cases, stable case keys, labels and provenance, mapping version, validation report. |
| Connection revision | Target identity, contract and secret reference; secret values never enter transcript or exports. |
| Dashboard revision | Canonical definition, registry/template versions, preview seed/generator version and review receipt. |
| Run manifest | Exact approved revisions, target, case selection, repeat/retry policy, budgets and approvals. |
| Result/report revision | Frozen run/score references and evidence links; later scores do not rewrite a downloaded report. |

Typing a new message must not implicitly start another evaluation or lose the selected one. A server mutation returns the object ID, revision, state and next permissible actions. Show “Saving…” until acknowledged; retain unsaved edits on failure. Handle simultaneous tabs using expected revision checks and a merge/review choice. A user can browse and run already validated deterministic evaluations if the authoring model is unavailable.

## 4. End-to-end screens and interaction contracts

### 4.1 Connect source or start from an API

The starting choices are **Git repository**, **Upload ZIP**, and **Connect hosted API**. Explain that source improves understanding but is not required for output-based evaluation. Local filesystem access is a trusted-developer CLI feature; it is not a browser intake option, even for a local deployment.

Git capture: repository URL, branch/tag/commit, subdirectory if needed, authentication reference for private repositories. Show the resolved immutable commit after import. ZIP capture: selected filename/size, uploading progress, verifying archive, extracted source summary and snapshot digest. A cancelled or failed import leaves a recoverable draft and does not create a runnable agent.

Ingestion errors have a typed stage and concrete next action: authentication failed → update repository credential; ref missing → choose a ref; oversized/unsafe archive → replace archive; unsupported language → inspect available adapter contract; parse error → file/line and re-upload; missing entrypoint → select or declare invocation. Never repair the original repository silently. Keep the deliberately broken reference fixture unchanged and explain its parse failure.

Analysis is read-only. Build/smoke execution follows the local sandbox boundary and the approval policy in the master plan. A “reference only” source never needs to build merely to help author output metrics for a hosted target.

### 4.2 Review and remember the agent

The reference panel answers: what the agent does, its workflows/agents, input/output shape, model/tool/service dependencies, potential side effects, observed evidence, limitations and questions. Each knowledge claim carries the canonical status `observed_static`, `user_confirmed`, `observed_execution`, `inferred`, `unknown` or `contradicted`, plus a source file/line or verification trace when available. Friendly labels such as “Found in source” and “Observed in execution” map to these exact stored states. Keep this separate from capability availability (`observed`, `declared`, `unavailable`, `unknown`) and evidence provenance (`proxy`, `adapter`, `connector`, `remote_reported`, `human_label`). Confidence applies to an inference, never to whether a run occurred.

Enumerated questions use buttons, radio groups or checkboxes in chat. Free text remains available. Actions are **Confirm understanding**, **Correct**, and **Continue with gaps** when those gaps do not block the selected evaluation. Corrections create a draft report revision and show a concise diff. Confirmation persists actor, time and report digest.

A later source update displays what changed and which evaluation assumptions require review. Existing runs retain the old report. For hosted APIs, show **Source provided for reference; deployed version not verified** unless a verified version mechanism links the deployment to that snapshot. A user-entered release label is a declaration, not attestation.

### 4.3 Select and define metrics in chat

Ask “What should this agent reliably do?” and suggest a small relevant set. Each option includes a plain-language statement, reason for recommending it, evidence required, and one of **Available**, **Needs labels**, **Needs trace integration**, or **Unsupported by this target**. Multi-select does not send multiple independent chat turns. A single **Add selected metrics** commits the selection proposal. Users can type another criterion alongside the selected options.

Metric cards must make the executable meaning inspectable:

| Field | User-facing control/description |
|---|---|
| Target and scope | Final response, request timing, selected tool/event, conversation, or run; case scoring vs run aggregate clearly separated. |
| Evaluator | Deterministic rule first; exact/structured comparison, closed trace rule, scalar predicate, or semantic rubric as supported. |
| Missing evidence | Explicit selection for `on_missing`; no preselected default. Explain with a no-response/no-tool example. |
| Evaluator error | Explicit selection for `on_error`; separate from an agent failing its criterion. |
| Per-case scoring | Boolean or declared numeric scale and what its endpoints mean. |
| Aggregate | Mean, pass rate, percentile or supported reducer; show eligible/scored/skipped/error denominator policy. |
| Gate | Threshold and enforcement eligibility; presentation settings never define gates. |
| Evidence prerequisite | Label fields, observable event kinds, attribution, timing source and completeness. |
| Semantic judging | Rubric version, judge binding and canonical readiness `UNCALIBRATED`, `CALIBRATING` or `CALIBRATED`; scores without current calibrated readiness are provisional and cannot enforce gates. |

“Never refund before eligibility” needs both an action-order rule and an explicit decision about cases with no refund action; a separate task-success metric catches an agent that does nothing. “Helpful” needs a reviewable rubric. “Cost” needs authoritative usage/price provenance; an output-only hosted response cannot supply it by implication. Custom requests that cannot be expressed with approved evaluators become a named unavailable proposal with a supported alternative, not generated executable code. R1 also retains an advanced operator path for an explicitly supplied, digest-pinned custom evaluator artifact: the UI selects an already approved artifact and its typed contract; execution occurs in a separate restricted evaluator sandbox. Do not offer chat-generated evaluator source, an evaluator SDK/marketplace, or execution in the API, worker or agent container.

### 4.4 Review the dashboard with sample data

The dashboard authoring skill selects from reviewed templates and the installed component registry. It emits canonical dashboard JSON. It never writes agent-generated HTML, JavaScript, CSS, arbitrary URLs, or evaluation expressions into a rendering escape hatch. Template assets are maintained code; extending the registry is an engineering change.

The preview uses the **same renderer, definitions, filters and selection contracts** as real results. Data comes from the versioned deterministic generator specified by v3 §14A. It is derived from declared expectations/ranges and seeded by the approved spec content hash. An undeclared range produces a placeholder. Metric proposals or inferred dataset labels are never portrayed as measured outcomes.

Persistent banner: **“Preview — synthetic examples of the dashboard. These are not evaluation results.”** No pass/fail verdicts, traffic-light outcome colors, or headline predicted scores. Synthetic evidence rows link to synthetic detail clearly marked as examples; they cannot masquerade as run evidence.

Chat changes such as “show latency beside success rate” create a new draft definition, validate bindings and update the preview. Show a concise component/binding diff, undo by creating another revision, and retain the previous accepted version. Changes to metrics or labels invalidate the corresponding preview review receipt. Layout-only changes do not mutate evaluation gates or already approved execution inputs.

Review identity includes definition hash, spec/dataset context, registry/template and generator versions. **Accept dashboard** accepts presentation. **Review run** is a separate action. The derived `preview.html` inside the required persisted artifact bundle is produced by the trusted renderer and remains visibly synthetic; it is not an independently editable source of truth.

### 4.5 Upload or author cases and golden labels

Offer **Upload CSV/JSON/JSONL**, **Add cases**, and **Generate draft cases**. Parse server-side under documented size limits. The mapping preview displays original columns/paths next to canonical case key, input, expected output, expected tool arguments, tags and conversation fields. Null, empty string, missing fields and malformed JSON remain visibly distinct. Show representative rows and a downloadable row-error report before applying a mapping.

The dataset table supports inspect, filter, add/edit and selecting a subset for a run. Fields inferred by the harness are badged `inferred`; user labels are `user_stated`; trace-derived cases/labels carry source run/evidence references. Explicitly distinguish expected values from the model response under test. An agent's own generated answer is not a golden label without an independent label decision.

Validation checks stable unique case keys, input shape, required labels per metric, contradictory labels, duplicate cases, invalid tags, unsupported protocol interactions, and fixtures needed for side effects. A bad row does not silently disappear from the denominator. Let the user correct it, exclude it explicitly into a new revision, or cancel the import. Show exclusions in the dataset receipt.

Generated cases are drafts for review, with nominal, boundary, invalid-input, refusal and relevant adversarial scenarios. Coverage indicates what has actually been represented; do not claim exhaustive branch coverage from source inference. Scripted multi-turn/approval-resume cases and specialized retrieval metrics are R2 and remain disabled in R1 with a clear release/capability explanation. R1 may evaluate recorded approval-order invariants and final-answer quality with its supported evidence; this does not enable interactive sessions or specialized retrieval scoring.

### 4.6 Choose and verify where the agent runs

**Hosted API (R1 synchronous stateless JSON):** URL, method, version/release label, timeout, credential reference, request-body mapping, response/output mapping, allowlisted headers, case/request identity and side-effect declaration. Reject stateful/session, streaming and asynchronous-job contracts as unsupported in R1. Authentication options are none, bearer or one named API-key header; OAuth refresh and mTLS require later connector work. Provide a redacted request/response preview and an owner-supplied example. Map fields using a closed declarative grammar; do not ask the LLM to emit request-construction code.

The connection card renders the canonical `draft → configured → verifying → verified` lifecycle, named failure states and later `stale`/`disabled`. Friendly labels must not collapse failures into success. Show verification expiry (24 hours by default) and invalidate it when configuration or secret versions change. Verification invokes a user-approved representative request and checks authentication, transport, response shape, mapping and a coherent final response. It is not a quality score. Show time, connection revision, measured round-trip duration and exact checks passed. Invalid credentials, authorization denial, rate limit, timeout, disallowed destination, invalid JSON, oversized response and mapping failure have separate recovery actions. A stateless HTTP request can still cause an external side effect: an unknown outcome cannot be retried as if no action occurred. Default to zero speculative invocation retries; show reconciliation/cancel options per the connector contract. R2 stateful targets additionally require isolated session initialization/reset/cleanup.

Hosted capability disclosure is adjacent to metric selection:

| Hosted observation | Permitted claim |
|---|---|
| Final response + client timing only | Output correctness/rubric scores, request success and externally observed latency. No internal model/tool timing, delegation, token spend or provider budget enforcement. |
| Target reports token counts/cost | Display as target-reported with provenance; no automatic promotion to authoritative metering. |
| Supported remote trace enrichment | Trace-based metrics only for verified event coverage, identity and completeness. Evidence remains attributed to its actual source. |
| Reference source lists a tool | Capability hypothesis; not proof the deployed endpoint called that tool. |

Credential fields are write-only; show saved secret name, scope and rotation state. Never echo secret values in chat, logs, artifacts or screenshots. Request validation errors preserve nonsecret mapping work. The browser calls the platform backend; the backend owns connector networking and authentication.

**Local isolated execution:** platform-aware setup card for supported rootless Podman configuration, image preparation, entrypoint/input/output declarations, dependency build log, allowed external destinations, fixture/world mode and budget policy. Do not label the existing trusted-development subprocess path “sandbox.” Do not promise Docker Desktop on macOS satisfies the locked architecture. Local execution is enabled only after containment and sole-egress recording proxy requirements pass. Show **Declare → Prepare → Smoke**, with classified failure recovery from v3 §4B.

Only local enforced execution can present the proxy's authoritative agent cost and hard budget as such. For hosted execution, the UI differentiates controlled platform request/judge spend from unknown remote internal spend. Switching execution mode recomputes eligible metrics and requests review of any dropped capability; it never silently skips trace metrics.

### 4.7 Review and run

The review card contains exact source/reference/connection/spec/dataset/dashboard revisions, selected case count, repeats, retry policy, fixture or live-side-effect mode, evaluator/judge bindings, limits, and the execution manifest digest. Show Quick/Standard/Full tiers within configured engine limits; estimates derive from available measurements and declare uncertainty rather than guarantee v3's illustrative timings. For hosted targets, unknown remote cost is explicit.

Readiness checklist consists of real object states: validated spec, dataset mapping/labels, reviewed preview, valid target verification, capability compatibility, execution safety policy and budget. Disabled run actions name the blocker and deep-link to its recovery surface. Users may continue other authoring work while a build or smoke is pending.

**Run evaluation** approves the displayed manifest. Any executable input change invalidates that approval. Queueing uses the same idempotency key through double click, request retry and reload. UI success appears only after the server returns the run ID/state. Cancellation first shows **Cancellation requested**, then the engine's observed terminal state. A local successful stop does not claim a remote operation was undone; render `remote_execution_unknown` for an in-flight request without a conclusive cancellation acknowledgment.

### 4.8 Follow results and investigate evidence

A persistent run card shows target, current stage, completed/total cases and attempts, heartbeat, estimated remaining time when defensible, measured platform cost, and cancellation. Leaving the page does not stop work. Streaming results update their affected blocks without resetting sorting, selection, scroll position or keyboard focus. Reconnection shows the last update time and reconciles from an authoritative snapshot/cursor; a missing event is not a fabricated state transition.

Keep these meanings distinct across copy and badges:

| Surface state | Meaning |
|---|---|
| Synthetic preview | Layout/data-shape review; no execution. |
| Connection/smoke verified | One technical invocation passed checks; no quality conclusion. |
| Evaluation running | A versioned test set is executing and/or scoring. |
| Partial results | Some scheduled work or aggregation is incomplete; no final verdict. |
| Complete evaluation | Execution terminal and all required metric aggregates complete; quality verdict may pass or fail. |
| Interrupted/cancelled | Preserved evidence may be useful; remaining cases remain unmeasured. |
| Refreshing results | Retrieving stored/current run state; not starting another evaluation. |
| Ongoing evaluation | A separately configured trigger/policy creates new runs; a connected API alone does not start monitoring. |

Summary cards show score plus denominator, metric gate, uncertainty/sample size when available, skipped/error counts and provenance badges. “Not observed” is not `0`; `ERROR` is not `FAIL`; uncalibrated judging is provisional and excluded from enforced gates. Show harness, agent-under-test and judge spend separately when each is available.

Cases table supports status, metric, tag, repeat and retry filters. Selecting a result opens expected/actual, scoring rationale, evaluator version and linked evidence IDs. Trace details show causal parents, source, relative time, truncation/redaction markers and event completeness. Adapter-sourced tool events are first-class evidence, never hidden by a proxy-only filter. Harness activity has its own page/stream label and is never merged into the evaluated agent's timeline.

Evidence deep-links carry run, case, attempt, score revision and event identity. Expired retained payloads show the surviving metadata and retention explanation; never regenerate “equivalent” evidence. Failure explanations cite these links and qualify uncertainty when evidence is partial. Rule-based failure groups are R1; embeddings-based clustering remains deferred.

### 4.9 Compare, re-score, export and return

**Compare runs:** choose baseline/candidate, show source/deployment and evaluation versions first, then server-computed comparability. Matching case keys and compatible metric/judge bindings define the paired subset; disclose changed inputs, excluded cases and sample sizes. Show regressed/improved/unchanged/insufficient-evidence states. Incomparable runs may be inspected side by side with the reason, but cannot produce an authoritative regression gate. Keep stochastic repeats and confidence information visible. Re-running only failures creates a named subset run, not a replacement for the full denominator.

**Re-score:** select stored evidence and a validated evaluator/spec revision, preview the scope, cost and gate impact, then create a new score revision without agent execution. Disable when retained evidence is insufficient. **Dispute/override:** preserve original machine score, reason, actor and new revision. **Label examples:** a small calibration flow is allowed; do not grow a new human review queue into R1. Render `UNCALIBRATED → CALIBRATING → CALIBRATED` from persisted judge readiness. The first ten labels only bootstrap the set; they do not prove calibration. Use the master/v3 label-count and κ thresholds; drift returns a binding to an existing uncalibrated/calibrating state rather than inventing a fourth state.

**Export:** choose summary report, scored-case CSV, machine-readable JSON, or evidence/artifact bundle as supported by the master contract. Show included objects, active filters versus full run, redaction policy, size estimate and snapshot revision. Generated reports pin run/spec/dataset/connection/score versions and include status, missing data, provisional badges, comparison limitations and evidence references. CSV escaping prevents spreadsheet formula interpretation. Credentials and hidden raw sensitive payloads are excluded. A standalone HTML report is generated by trusted templates and clearly distinguishes historical results from a synthetic preview; an online “live dashboard” requires its authenticated platform connection.

The project overview offers the latest completed evaluation, active runs, last successful target verification and known regressions. “Refresh” retrieves state. “Run again” creates an explicit new run. Existing API/webhook trigger capability may be surfaced as read-only trigger status; a schedule/pipeline configuration product is not implied by the word live.

## 5. Component vocabulary and presentation contract

Use the installed registry as the machine-readable source for available components, versions, inputs, selections, accessibility semantics and payload schema. Conceptual component names from v3 are not permission to silently introduce aliases incompatible with the current lowercase registry. Migration defines canonical IDs and preserved versions.

| Component family | Responsibility and essential states |
|---|---|
| Reference/understanding card | Claims, citations, corrections, confidence and confirmed revision; unknown must remain unknown. |
| Metric option/editor | Stable metric ID, selected state, required evidence, explicit missing/error policies; invalid/unsupported explain why. |
| Evaluation plan / approval | Pinned manifest, review receipt, expiry/conflict and server state; no autonomous acceptance. |
| Metric summary/breakdown | Scored denominator, gate eligibility, aggregate state, provenance; empty/partial/error/provisional. |
| Case table / expected vs actual | Typed cells, selection contract, paging, filtering, labels and revisions; empty vs no matches distinguished. |
| Run summary/comparison | Run state and comparable subset; never treats a numerical delta alone as a regression. |
| Trace timeline / tool inspector | Fetch detail only for selected evidence; source attribution, missing/truncated payload, accessible event list. |
| Coverage / rule-based failure group | Declared coverage basis and group rule; no exhaustive or semantic claim without evidence. |
| Distribution/time series | Available measured observations only; unit/denominator, missing intervals and accessible tabular equivalent. |
| Filter bar / insight panel | Bound filters and evidence-backed explanation; stale/unsupported references expose a recovery state. |

Each registry component is self-contained and data-bound. The application shell supplies routing/authentication; it must not calculate a second hidden KPI set outside the definition. Dashboard summary loading uses pre-aggregated data. Raw trace detail is lazy, separately paginated and scoped to the selected run/case. Unknown registry versions or bindings produce a named quarantine state with migrate/review, not silent substitution.

## 6. Cross-cutting loading, recovery and accessibility

Every asynchronous surface implements empty, loading, ready, stale, validation-error, permission-error and service-error states where applicable. Initial load may use stable skeletons; refresh preserves last known data with timestamp. Errors include a correlation ID, concise cause, retryability, retained work and the next action. Keep technical diagnostics expandable. A server failure is not rendered as an empty project list.

For chat: keep composing available during background tasks; serialize conflicting artifact mutations. Show queued edits and allow cancellation before execution. Do not auto-scroll away from a user reading history; offer a new-messages indicator. Interrupted model generation retains the last persisted draft. Tool summaries describe observed actions/outcomes rather than exposing internal reasoning text or inventing a staged “agent team” performance.

Use the house [apple-design skill](../../.agents/skills/apple-design/SKILL.md): prompt pointer-down feedback, native release/keyboard activation, interruptible springs from presentation value, compositor-only `transform`/`opacity` motion, and directionally consistent panels anchored to their triggers. Keep blur constant during animation to respect the compositor-only project constraint. Calm critically damped motion is the default; momentum-driven overshoot belongs only to an actual gesture. New transitions must not lock input.

Retain Traceline's existing teal/slate identity as the starting palette (`web/styles.css:9`). Use system UI typography for dense surfaces, tabular numerals for scores/times, monospace for IDs/code, and heading tracking/leading appropriate to size. Make hierarchy through grouping and weight before adding card chrome. Translucency belongs to navigation or a floating companion panel; dense data stays on a legible surface. No external visual identity was needed for this plan, so no third-party brand skin is introduced.

Accessibility acceptance is behavioral:

- Every pointer action works by keyboard; tabs and list selections follow their semantic keyboard pattern. Native buttons respond to Enter/Space without an additional pointer path causing duplicate commits.
- Focus enters dialogs, stays within modal tasks, and returns to the trigger; nonmodal artifact panels do not trap focus. Escape closes dismissible overlays without discarding persisted work.
- Radio groups/checkboxes expose selected and disabled reasons. Inline validation has programmatic labels and references. The chat composer has a real accessible label, not only placeholder text.
- Screen readers hear meaningful stage changes through polite live regions, not every token, score tick or trace event. Critical failures are announced once with a reachable recovery control.
- Color is redundant with status text/icon. Charts have table/text equivalents. Dense trace graphs also expose an ordered event list. Long identifiers can be copied without hover-only controls.
- Reduced motion uses static or brief opacity changes; reduced transparency uses solid surfaces; increased contrast uses stronger outlines/foreground contrast. Test combinations with light and dark themes, not preferences individually only.
- Layout survives enlarged text and browser zoom. Touch targets and spacing remain usable; dragging has move-up/down or other keyboard alternatives. No feature requires a precision gesture.

Responsive composition: at wide desktop widths show chat and artifact side by side; at intermediate widths allow switching the active panel with persistent context; at narrow/mobile widths show a single primary surface with explicit **Chat / Plan / Results** navigation and a return link. Tables retain essential case/status columns, moving additional details into the case view; only the table scrolls horizontally when needed. Do not shrink all columns until text becomes unreadable. Keep the composer visible with a software keyboard and safe-area insets. Mobile supports review, simple edits, run status and evidence; advanced mappings may use a focused full-screen editor but must remain operable.

## 7. Delivery acceptance scenarios

These scenarios are required browser acceptance evidence, not tests already run. The implementation agent should record test data, environment, viewport, actual observations and screenshots/video for each delivered slice.

| ID | Scenario | Observable pass condition |
|---|---|---|
| UX-01 | Import a valid unrelated Git repo and ZIP | Correct source identity, no sample-domain substitutions, report citations and persisted snapshot. |
| UX-02 | Import the deliberately broken reference fixture | Parse failure named before execution; file/line recovery; original fixture unchanged. |
| UX-03 | API-only agent with no source | Output metrics usable after contract verification; internal tool/cost metrics visibly unavailable. |
| UX-04 | Reference code differs from deployment | Unverified source/deployment relationship persists on reference, run and export. |
| UX-05 | Correct understanding and reload | Exact confirmed revision, unresolved question and chat decision survive; no duplicate import. |
| UX-06 | Select suggestions plus a custom metric | One reviewed selection, explicit missing/error semantics and scoped validation errors. |
| UX-07 | Change preview by chat, then undo | Same registry renderer, deterministic samples, persistent synthetic banner and immutable revisions. |
| UX-08 | Malformed/partly labeled dataset | Mapping report names rows; no silent exclusion or invented golden labels; selected remediation creates a new version. |
| UX-09 | Hosted verification returns 401, timeout, wrong shape or a stateful contract | Correct typed failure; no ready badge; mappings persist, unknown side-effect outcome is not retried blindly, and R1 rejects unsupported session/stream/async protocols. |
| UX-10 | Local setup lacks required containment | Execution disabled with precise setup reason; no subprocess-to-sandbox claim. |
| UX-11 | Approve a run, then edit a bound revision | Old manifest cannot execute new inputs; review needed and approved identity inspectable. |
| UX-12 | Double activation/reload during queueing | One run under the same idempotency key; observed queued/running state. |
| UX-13 | Run streams while user reads a failure | Selection, scroll and focus stay stable; per-metric partial state remains until complete. |
| UX-14 | Drop connection then cancel/interruption | Stale timestamp, authoritative reconciliation, requested-vs-observed cancel, incomplete results preserved. |
| UX-15 | Compare incompatible datasets/judge bindings | Paired subset/exclusions visible; regression enforcement disabled when invalid. |
| UX-16 | Re-score and override | No agent rerun; original score/evidence preserved; new score revision and actor/reason visible. |
| UX-17 | Export filtered partial provisional results | Export states filters, partial denominator and provisional status, pins revisions and contains no secrets. |
| UX-18 | Authoring model unavailable | Stored artifacts/results remain usable; deterministic valid runs/re-scoring still available. |
| UX-19 | Keyboard/screen reader across whole R1 flow | No pointer-only action, focus trap, inaccessible option or unannounced blocking error. |
| UX-20 | Mobile, text zoom, themes and preference combinations | No unreachable actions or lost context; no mandatory gesture or vestibular transition. |
| UX-21 | Registry mismatch and stale response race | Named migration state; late responses cannot replace a newer selected evaluation/run. |
| UX-22 | Full end-to-end rehearsal | Approved preview definition is the definition used for the actual run dashboard and exported report. |

Before closing an implementation milestone, load the actual application with Playwright, exercise its interactions, and inspect screenshots at desktop and mobile sizes. Review moving panels and live result updates at normal speed and in slow/frame-by-frame playback. Include light/dark, reduced-motion, reduced-transparency, increased-contrast and keyboard checks. Do not mark behavior complete from source review alone; record startup or external-service blockers plainly.
