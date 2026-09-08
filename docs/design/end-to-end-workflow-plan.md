# Traceline — End-to-End Workflow and Delivery Plan

Date: 2026-09-08. Status: implementation-ready product direction; implementation remains unfinished.

## 1. Read this first

**The requested product is well aligned with v3's vision, but only partially implemented.** The intended product is an evaluation assistant that helps people understand an agent, define what good behavior means, prepare representative tests, review a dashboard, connect an execution target, run controlled evaluations, and investigate measurable results. The assistant authors and explains; the engine validates, executes, scores, and preserves evidence.

This document incorporates the owner's September 8 workflow clarification. It is a dated extension to [v3](LLM_Agent_Evaluation_Engine_v3.md), not a replacement for its detailed scoring and isolation contracts. Read [the original review](v2-review-and-v3-plan.md) for the reasons behind those contracts. Where those documents disagree, the original review still governs existing mechanisms. The explicit new scope decisions here are hosted-agent connections, source-as-reference, durable dashboard artifact bundles, and a nontechnical authoring journey. They do not reopen the other accepted scope cuts.

The package has three parts:

| Document | Purpose |
|---|---|
| This file | Requirements, alignment audit, architecture, contracts, use cases, release gates |
| [UI/UX specification](workflow-ui-ux-plan.md) | Screens, chat interactions, component behavior, accessibility, UI recovery states |
| [Implementation checklist](workflow-implementation-plan.md) | Ordered tasks, ownership boundaries, files, dependencies, executable acceptance scenarios, agent handoff |

The checked-out repository is **not design-only anymore**. At review time HEAD was `141ec50`, v3 existed, and Python backend, web assets, tests, and uncommitted implementation changes existed. The supplied AGENTS.md description of repository maturity is stale. Source observations below include the working tree, not just HEAD; file positions may change after concurrent edits. No existing implementation or reference fixture was changed for this planning task.

Existing verification: `PYTHONPATH=src python -m pytest -q` completed with **513 passed, 3 skipped, 3 deselected, 2 warnings in 31.79 seconds**. The configured default excludes live-provider tests. This verifies the existing test baseline, not production readiness, remote connections, sandbox security, or all three operating systems. The README's 508-test count is stale too.

## 2. Decisions that make the workflow coherent

1. **Code and execution target are separate.** A repo/ZIP describes what the assistant should understand. The evaluated target may be that code in a sandbox or a separately hosted API. A repository hash does not prove which version is deployed remotely.
2. **Chat is the authoring surface; versioned objects are the memory.** Save the confirmed understanding, metric definitions, datasets, dashboard revisions, connections, and run manifests. Do not reconstruct configuration by re-reading an unbounded transcript.
3. **Suggested metrics are selectable cards.** Users can choose suggestions, add their own intent, edit thresholds and missing-data policies, then review a plain-language specification before execution.
4. **A skill guides dashboard composition from reviewed templates.** It emits validated dashboard JSON. A trusted renderer produces the HTML preview and the real dashboard from the same definition. The model never emits executable HTML/JavaScript for the application to run.
5. **Preview, connection verification, evaluation, and monitoring are distinct.** Sample data illustrates a layout. A smoke request proves an invocation works. A measured run tests a dataset. Monitoring requires scheduled runs or a production-event integration; connecting a URL does not create production telemetry.
6. **Evidence availability limits metrics.** A basic API exposes inputs, outputs, externally measured timing, and transport outcomes. Internal tools, retrieval, delegation, tokens, and cost require explicit telemetry or controlled local execution. Unknown values are never zeroes.
7. **Local uploaded code requires the v3 sandbox.** Rootless Podman with a fresh container per case attempt and proxy-only egress is the shipping path. Compose starts the platform; a single container containing the whole app is not per-agent isolation.
8. **A complete release is a sequence of verified increments.** The final implementation prompt may launch the whole sequence, but the agent must checkpoint, test each increment, and stop on real external blockers. One prompt is an entry point, not proof that one uninterrupted generation can safely finish the system.

### 2.1 Alternatives considered

| Approach | Advantage | Tradeoff / decision |
|---|---|---|
| Extend the existing deterministic engine with durable authoring and two target adapters | Preserves tested scoring and evidence concepts; fits the user journey | **Selected**; replace prototype boundaries incrementally |
| Build a dashboard generator first, connect APIs later | Fast visual demonstration | Cannot establish valid scoring, observability, or reproducibility; insufficient as the core product |
| Replace the repository with a framework-specific evaluation stack | Could accelerate one framework's integration | Narrows framework coverage and introduces framework coupling; conflicts with the harness decision |

### 2.2 Changes from the previous product framing

| Previous position | Clarification in this plan | Reason |
|---|---|---|
| Developer is the wedge; operations mostly consume results | Developer supplies integrations, but nontechnical operators must be able to author and run approved suites | Explicit owner request; one shared product, not a separate application |
| Repo is assumed to become the run image | Repo can be reference material for a hosted target; API-only onboarding also works | Separates knowledge from execution |
| “HTML preview” already corrected to declarative rendering | Preserve that correction; add a saved, versioned HTML export generated by the renderer | Satisfies the owner's folder/template requirement without restoring arbitrary frontend generation |
| Docker colloquially describes local execution | Use Compose-compatible installation plus the approved rootless Podman execution tier | Preserves the locked isolation design |
| General monitoring/replay deferred | Live updates for measured runs are core; recurring synthetic evaluation is R2; passive production monitoring remains deferred | Makes “live dashboard” an explicit promise |

## 3. Alignment audit against actual implementation

“Foundation” means relevant code exists and can be extended. It does not mean the entire product requirement has been delivered. These are static code observations plus the test run above.

| ID | Requirement | Current evidence | Assessment and required work |
|---|---|---|---|
| A01 | Git repo and ZIP intake | `api.py:315`, `api.py:1266`; no Git clone/import service found | ZIP foundation only; temporary extraction checks traversal but lacks complete caps, collision and provenance controls. Add immutable imports and Git jobs. |
| A02 | Understand the supplied agent accurately | `api.py:1154–1263`, `orchestrator.py:52–166`, `orchestrator.py:417–431` | Heuristic/sample-specific. Invalid source can fall back to sample; tool/model defaults are invented. Replace with evidence-backed discovery and explicit unknowns. |
| A03 | Durable confirmed understanding / memory | `api.py:1330–1400`, `storage.py:269–487` | Chat constructs a new orchestrator per request; no durable session/confirmed knowledge-version model found. Custom eval persistence is not conversation memory. |
| A04 | Conversational metric selection | `harness.py:190`, `orchestrator.py:167–275` | Validated model-authoring foundation exists separately; main chat follows keyword/template logic. Wire one authoritative stateful authoring pipeline. |
| A05 | Selectable suggestions with custom intent | Existing chat suggestions; UI companion gives positions | Click-to-send suggestions exist, but not a persisted multi-selection metric review. |
| A06 | Real smoke verification | `engine.py:252` versus `orchestrator.py:284–299`, `api.py:1261` | Engine smoke path exists; chat validator calls schema validation “SMOKE_PASSED”, analyze returns true without invoking. Unify readiness around actual verification records. |
| A07 | Skill/template-based dashboard | `dashboard.py:385`, `dashboard.py:1001`, `orchestrator.py:318` | Strong canonical registry/validator/resolver foundation; chat produces a different incomplete shape. No complete versioned skill/artifact workflow found. |
| A08 | Saved preview edits persist to real dashboard | `storage.py:1647`; UI separate preview and default run dashboard paths | Partial storage; unify definition identity and immutable revisions through preview, approval, and run display. |
| A09 | Uploaded datasets and golden references | `spec.py:580`, authored embedded cases | Case model exists. Dataset import, mapping, label review, independent dataset versions and exports require work. |
| A10 | Hosted agent API / authentication / verification | No target connector in `runner.py` or matching connection APIs | Missing as a product path. A platform model-provider gateway is not an agent API connector. |
| A11 | Local isolated execution | `runner.py:1–27`, `runner.py:142–170` | Explicit subprocess prototype; inherits parent environment. Fresh process does not isolate filesystem, network, secrets, or shared external state. Replace shipping runner boundary. |
| A12 | Proxy trace source and hard budget | `capture.py:1–30`, `engine.py:921–962` | Cooperative in-process trace writer, not intercepting proxy. Redaction-state and identity checks do not prove truthful events or remove sensitive content. |
| A13 | Deterministic scoring / repeats / revisions | `spec.py`, `evaluators.py`, `trace_rules.py`, `engine.py`, `judge.py` and their tests | Useful foundation. Preserve behavior with migration tests; audit semantic divergences before treating it as complete v3 compliance. |
| A14 | CEL, not a custom scalar language | `predicates.py:1–50` explicitly documents a hand-written parser | Diverges from locked CEL decision despite avoiding eval. Adopt a conforming pinned CEL implementation with versioned migration. |
| A15 | Multi-provider platform model gateway | `gateway.py:64`, `gateway.py:106`, `pyproject.toml` | Gateway abstraction exists; current concrete gateway is DeepSeek-specific, no LiteLLM dependency declared. Implement the approved SDK abstraction. |
| A16 | Multi-tenant schema with RLS | `storage.py:269–487`, `storage.py:738–781` | Workspace filters and a Postgres wrapper exist; no `CREATE POLICY` / RLS DDL found. Enforce tenancy in PostgreSQL, access control, artifacts, jobs and streams. |
| A17 | Measured live results and evidence | `api.py:801`, `api.py:883`, `api.py:903`, canonical dashboard resolution | SSE, trace reads and score revisions are foundations. Need durable workers, capability states and full authored-dashboard wiring. |
| A18 | Compare versions, export, recurring runs | No complete comparison/export/schedule API found in audited modules | Build immutable comparison and export services; add scheduling separately after R1. |
| A19 | Reproducible installation | Existing Dockerfile/Compose and Python manifest | App+database packaging exists, not the specified worker/proxy stack. `python-multipart`, required by ZIP routes, is not declared in pyproject. Pin and verify clean installation. |

Do not preserve misleading behavior simply because an existing test expects it. Change such tests to assert the user contract, while retaining the rest of the passing baseline.

## 4. End-to-end journey

### Step 1 — Create a project and connect material

Offer **Git repository**, **Upload ZIP**, **Connect hosted API**, and **Try example**. A project may contain several workflows/agents. The user selects one target workflow per evaluation; several developers' agents remain separate versions and targets within the workspace.

Git input includes URL and explicit ref; resolve and persist the full commit and source digest. Public HTTPS repos are R1; private HTTPS uses an encrypted token reference scoped to the fetch worker. Disable hooks, submodule recursion and LFS auto-fetch unless specifically enabled by an administrator in a later connector revision. Arbitrary local paths are a trusted-developer CLI feature, not a browser intake capability. ZIP uploads create a content-addressed source version, not a path the browser may later mutate.

An API-only project uses a manually supplied contract/OpenAPI import and examples as initial knowledge. Adding source later improves suggestions but does not invalidate existing remote results by itself.

OpenAPI import is schema assistance, not permission to invoke every documented endpoint. R1 resolves only bundled/local references within the bounded imported document; external `$ref` fetches are disabled. The user selects the operation, reviews mapping and configures its credential separately.

### Step 2 — Build and confirm understanding

Inspect files as data; do not install dependencies or import agent modules for discovery. Parse supported languages; collect entrypoints, framework hints, declared models, tools, retrieval components, external services, input/output shapes and workflows. Every finding has source location, confidence and status: `observed_static`, `user_confirmed`, `observed_execution`, `inferred`, `unknown` or `contradicted`.

Exclude `.env`, private keys, credential stores, `.git` internals, dependency/vendor directories and generated/binary artifacts from model context and search indexing. Preserve referenced environment-variable names, never their values. Scan permitted text for embedded secrets before model use; findings contain file location/category and masked evidence. Raw imports are access-restricted and encrypted while quarantined; the safe source manifest records excluded files and both acquisition and sanitized-snapshot digests. Do not silently include raw source or secret-bearing configuration in reports/exports. A local build receives only the approved sanitized snapshot and explicit nonsecret configuration; live credentials come through the proxy.

The assistant presents “Here is what I understand” with editable facts and unresolved questions. It asks only about material ambiguities: workflow selection, entrypoint, business success criteria, expected outputs and side effects. Users can correct the understanding in chat or edit a fact card. Confirmation writes an immutable knowledge version and a readable report. Unconfirmed facts remain available for exploration but cannot be presented as verified runtime capabilities.

A parse error blocks local build/run, not discussion or a hosted target. Source changes trigger a diff of affected facts; confirmed facts are re-reviewed where evidence changed. Old run manifests keep their old knowledge version.

### Step 3 — Define measurable success

The assistant suggests a short ranked set of metrics using confirmed behavior and the selected target's capabilities. Each card states the question, evaluator, evidence needed, missing-data treatment, estimated judge cost where relevant, and why it was suggested. Default suggestions: successful completion, output validity, latency, correct business answer, plus tool/workflow metrics only when observable.

Suggestions support multi-select, deselect and “Add my own metric.” Typed intent follows the same validation pipeline. For each metric establish its target, population/applicability, per-case result, run aggregation, required coverage and gate. Users review a plain-language summary with an optional structured view. No silent universal 90% threshold. “Not observable” means choose another metric or add instrumentation; it never means assume a pass.

### Step 4 — Prepare representative cases

Accept CSV, JSON and JSONL in R1. Show schema/column mapping, sample rows and rejected-row reasons before committing a version. Users may start with generated cases, uploaded golden data or manually entered examples. Infer inputs from the supplied contract, but label generated expected answers as **inferred** until reviewed. Unlabeled cases can still measure latency, structure and known invariants; they cannot establish answer accuracy.

For each workflow include a normal case, a boundary case, invalid input and an external failure where controllable. Generated cases are draft fixtures, not evidence of production coverage. Keep evaluation labels outside the agent-visible request. Define dataset tags, split, locale, scenario, stable case identity and reference provenance.

### Step 5 — Preview and refine the dashboard

The assistant loads the built-in dashboard skill and selects approved components/template assets. Generate a canonical definition tied to the current evaluation and dataset versions. A deterministic generator produces samples from declared shapes/ranges; missing ranges render placeholders. Display a permanent **Synthetic preview — no agent has been evaluated** banner and no pass/fail verdict styling.

“Show latency first,” “group by scenario” and “add a failures table” become validated definition revisions. Metric or case changes revise those objects too; the UI shows what changed. Persist the definition, preview data, renderer version, readable report and derived HTML in the project's artifact area. Refresh/reopen must recover the same draft. Reviewing a layout approves presentation; it does not authorize network calls or a batch run.

### Step 6 — Configure execution

Choose **Hosted API** or **Local sandbox**. These share datasets, evaluators, run lifecycle and dashboards through a target adapter contract. They expose different capabilities and reproducibility guarantees.

For a hosted API, configure an endpoint, request mapping, response mapping, authentication reference, timeout and rate/concurrency limits. Preview the redacted request. The user chooses a safe smoke input and starts verification. Test authentication, a representative invocation and output mapping; a health endpoint alone is insufficient. Persist exactly what was verified.

For local execution, choose/declare an entrypoint, dependency manifest, runtime profile, network policy, provider-secret bindings and fixture world. Build in an isolated builder, launch a fresh sandbox, and capture a real smoke result through the proxy. Record observed tool/model capabilities. Authoring and preview remain usable while preparation runs.

### Step 7 — Review and run

Show target identity, environment, verified date, dataset/version, case/repeat counts, selected metrics, unsupported metrics, label coverage, time/cost estimate, external-side-effect policy and hard limits. Quick is at most 20 cases; Standard normally 100–200; Full at most 5,000 unique cases, with a warning at 1,000. Repeat multiplier and maximum attempts are shown separately. Estimates are ranges derived from smoke/historical samples, never timing promises.

The user authorizes the frozen manifest. An existing authorization can cover exactly matching bounded scheduled/CI runs; any material change requires a new authorization. The backend checks versions and readiness again, writes the run and queue job transactionally, then reports the observed state. A button click or a valid draft is not success.

### Step 8 — Observe, explain, compare, export

Show executed/total cases, attempts, pending scoring, available cost, coverage and measured partial results as they arrive. The browser subscribes to platform progress; it never calls the hosted agent directly. Users may leave and return. Every metric has evidence links, provenance, numerator/denominator and unknown/excluded counts.

A terminal run with unfinished scoring is not a final dashboard. Cancel stops new work and records unfinished/uncertain requests. Failures link to actual/expected data and trace events. The assistant can summarize patterns and suggest a new test or metric revision; it cannot silently alter historical scores.

Compare compatible frozen runs, distinguish regressions from noise and changed coverage, then export CSV summaries, JSON results/specs, and a shareable HTML report with retained redacted evidence. Re-score preserved outputs/traces under a new score revision without re-running the agent. In R2, scheduled synthetic runs maintain a measured trend; passive production ingestion remains a separate future path.

## 5. Architecture and ownership

| Component | Responsibility | Must not do |
|---|---|---|
| Browser / trusted renderer | Chat cards, review, preview and results using declarative definitions | Execute model-authored scripts, hold service secrets, invoke agent endpoints directly |
| API / authorizer | Authenticated workspace context, typed commands, version checks, artifact access, SSE | Treat client-provided workspace IDs or approval flags as authority |
| Harness service | Use retrieved project knowledge to propose validated JSON changes and evidence-backed explanations | Score traces by narrative, execute repo instructions, claim unobserved tool completion |
| Import/discovery jobs | Safely acquire source, parse/index approved content, persist findings | Execute imports/setup.py/hooks while discovering |
| Version/artifact service | Immutable sources, knowledge, specs, datasets, dashboards and exports | Use mutable filenames as reproducibility identity |
| Target connector worker | Verify/invoke approved remote API, map responses, emit connector observations | Claim knowledge of unobserved remote internals |
| Build/runtime worker | Construct approved image and manage per-case sandbox lifecycle | Share agent filesystem/state/secrets with the control plane |
| Per-run recording proxy | Sole sandbox egress, credentials, redaction, metering, cassettes, fault injection | Accept shim claims as metered usage or authoritative ordering |
| Scoring workers | Deterministic evaluation, judge jobs, aggregation and revisions | Mutate historical result rows into a different evaluation |
| PostgreSQL + artifact store | Durable truth, RLS, jobs, evidence, retention | Expose unscoped rows or raw credentials to the harness |

Flow: browser → authenticated API → durable commands/jobs → discovery/harness or execution adapter → redacted observations → scoring → aggregate read models → same dashboard renderer. Harness messages/tool calls live in their own stream; evaluated-agent traces live in another. Platform operational logs are a third stream. A correlation ID links them without merging their meaning.

Extend the present FastAPI/Pydantic backend and tested semantic modules. Use PostgreSQL as the shipping database, a PostgreSQL leased job queue/outbox per v3, and a workspace-scoped artifact-store interface backed by a persistent local volume initially. An S3-compatible backend is optional, not required for the first install. Preserve the existing frontend until incremental specialist-owned modules replace it; a framework rewrite is not a prerequisite.

### 5.1 Harness behavior and context memory

Use one conversational coordinator with typed read/propose/validate/commit tools and a ModelGateway. Optional bounded read-only analysis helpers remain as in v3; displaying named “agents” does not constitute a working orchestration design. Do not couple the harness to CrewAI, LangGraph or another evaluated framework.

Each turn loads: system/skill versions; confirmed project-knowledge summary; current evaluation/dataset/dashboard/target version IDs; unresolved questions; recent redacted messages; relevant source snippets with citations. Long history is summarized into versioned, evidence-linked state. Retrieval is workspace-scoped in the query. Source text, README instructions, comments, datasets, remote output and retrieved passages are untrusted data, never higher-priority tool instructions.

The model emits structured JSON conforming to a tool schema. Validate identifiers, types, CEL and dashboard bindings; allow at most two bounded repair attempts, then return a typed error and keep the previous valid draft. Mutation tools use `draft`, `validated`, `queued`, `running`, `failed` and `rejected` as observed outcomes, extended only by their specific lifecycle. Responses contain concise action summaries and evidence, not simulated internal reasoning.

## 6. Durable contracts

These are target contracts to implement, not claims that current routes accept these shapes. Reuse v3 EvaluationSpec, TraceEvent, metric semantics, dashboard registry and binding grammar. Add versioned envelopes around them and an explicit compatibility migration for existing prototype records.

### 6.1 Objects and invariants

Every persistent object/table includes `workspace_id`; foreign references must remain within it. Authoring versions are immutable with parent ID, content digest, creator, provenance and timestamp. A small mutable pointer identifies the active draft; updates require `expected_revision` to prevent lost edits.

| Object | Required additional content |
|---|---|
| SourceVersion | kind, repo URL/ref/resolved commit or ZIP digest, source digest, safe artifact URI, import job ID |
| KnowledgeVersion | source-version refs, workflow IDs, confirmed facts, unresolved facts, citations, redaction version, report artifact |
| HarnessSession / Message / ToolCall | project, version pointers, redacted content, kind, operation ID, observed outcome, confirmation ref |
| EvaluationVersion | v3 spec, knowledge version, immutable metric/evaluator bindings and dataset version, validation report |
| DatasetVersion | schema, case rows, input hashes, reference provenance, label status, tags, data split, import validation report |
| TargetVersion | kind, source/image or connection version, workflow, capability profile, reset policy, environment, declared deployment version |
| ConnectionVersion | approved endpoint policy, typed mappings, secret reference, limits, protocol version, verification-policy version |
| VerificationRecord | target-version hash, test input hash, secret-version ref, observed identity/capabilities, outcome/error, measured time, expiry |
| DashboardVersion | canonical definition, registry/template/skill/renderer versions, spec/dataset refs, preview artifact refs, parent |
| RunManifest | all frozen version refs, case population, repeats/retries, budgets, isolation tier, world seed/cassette, observed/declaration provenance |
| Run / Attempt / Evidence | execution state, aggregation state, attempt authority, source/trust, payload caps, redaction and retention state |
| ScoreRevision | run/evidence refs, metric/evaluator/rubric/model/price versions, results, provenance, prior revision |
| Comparison | both runs and score revisions, case intersection, comparability report, estimates/intervals, gate outcome |
| ExportJob / ExportManifest | frozen refs, field selection, artifact hashes, redaction, retention limits, completion status |
| Authorization / Audit / Job | actor, exact operation and manifest hash, limits/expiry, idempotency key, lease/fencing state, observed result |

### 6.2 Knowledge fact example

```json
{
  "fact_id": "fact_eligibility_tool",
  "kind": "tool",
  "name": "check_refund_eligibility",
  "status": "observed_static",
  "confidence": 0.72,
  "evidence": [{"source_version_id": "src_01", "path": "agent.py", "line_start": 40, "line_end": 58}],
  "confirmed_by": null,
  "observed_in_attempt_id": null
}
```

The number is an analysis confidence indicator, not a statistically calibrated probability. A runtime dict can justify an inferred tool candidate; it does not establish an executed call. Confirmed knowledge is domain reference memory, not a chain-of-thought archive.

### 6.3 Hosted connection example and mapping grammar

```json
{
  "schema_version": 1,
  "kind": "http_json",
  "endpoint": "https://agent.example.com/evaluate",
  "method": "POST",
  "auth": {"type": "bearer", "secret_ref": "secret_agent_staging_v2"},
  "request": {
    "constants": {"mode": "evaluation"},
    "bindings": [{"to": "/query", "from": "/input/query"}]
  },
  "response": {"output_pointer": "/answer", "deployment_version_header": "X-Agent-Version"},
  "timeout_seconds": 120,
  "max_response_bytes": 1048576,
  "max_concurrency": 2,
  "requests_per_minute": 30,
  "retry_max": 0,
  "environment": "staging",
  "side_effect_policy": "test_environment",
  "session_mode": "stateless",
  "network_policy_ref": "policy_public_https_v1"
}
```

Mappings use JSON Pointer to copy values from the allowlisted agent-visible envelope (`input`, approved session fields, generated invocation ID), plus literal constants. No string interpolation engine, scripts, functions, SQL or access to `expected`. Reject overlapping destination pointers, invalid pointers, missing required inputs and conflicting constants. Empty response pointer selects the whole JSON value; otherwise the selected value must satisfy the declared output schema. Request/response preview is redacted and uses the exact mapper invoked by workers.

R1 authentication: none, bearer, or one named API-key header via encrypted secret reference. Header names are allowlisted; forbid user overrides of host, content length, proxy headers and reserved authorization fields. OAuth token-refresh flows, mTLS and arbitrary protocols are R2 or separate connector work. Secrets are entered in a dedicated control and never echoed into chat, source reports or exported bundles.

### 6.4 Capability and trust model

```json
{
  "target_version_id": "target_01",
  "profile": "remote_output_only",
  "capabilities": {
    "final_output": "observed",
    "end_to_end_latency": "observed",
    "tool_execution": "unavailable",
    "retrieval": "unavailable",
    "delegation": "unavailable",
    "token_usage": "unavailable",
    "provider_cost": "unavailable"
  },
  "evidence_source": "connector",
  "verified_by": "verification_01"
}
```

Capability status is `observed`, `declared`, `unavailable` or `unknown`. Provenance is independently `proxy`, `adapter`, `connector`, `remote_reported` or `human_label`; never overload provenance as availability. Connector-owned HTTP/timing observations are authoritative for that boundary. Remote-reported internal events may support metrics but are labeled self-reported; signed delivery proves sender identity, not event truth. Local adapter events are first-class tool evidence and re-redacted at ingress, while proxy events win conflicting network/cost claims.

Each metric names required capability fields. Validation returns `ready`, `needs_labels`, `needs_instrumentation`, `needs_verification` or `invalid` with affected metric IDs. A target without required evidence cannot run that metric as an enforceable gate. Unknown event absence cannot be interpreted as a successful “never” check.

### 6.5 Run manifest and authorization

The manifest freezes source/knowledge/target/connection/evaluation/dataset/dashboard versions; exact case IDs; repeats and retry policy; runtime-image digest; adapter/proxy/decoder versions; seed/world/cassette; judge binding; redaction/price-table versions; budgets and concurrency. Remote versions carry `declared_version`, `observed_version`, `source_alignment` (`verified`, `asserted`, `unknown`, `mismatch`). An unknown deployment identity remains visible in the report and limits reproducibility.

Authorization stores the canonical manifest hash, actor, allowed action, expiry and optional bounded reuse policy. Preview review is a different record. A changed endpoint, secret version, case population, metric gate, image or budget invalidates execution authorization. Backend preflight validates it again at dispatch. Idempotent repeated submissions return the original run rather than spending twice.

## 7. Hosted execution in detail

### 7.1 Verification lifecycle

Connection states: `draft → configured → verifying → verified`; failures `auth_failed`, `unreachable`, `protocol_error`, `mapping_invalid`, `timeout`, `policy_rejected`; later `stale` or `disabled`. Verification is valid for 24 hours by default and always bound to exact configuration/secret versions. Revalidate DNS/network policy on every call even while the verification record is fresh.

Verification checks scheme/host/IP policy and TLS, authentication and HTTP response status, representative invocation, output mapping/schema, response-size limits, version identity, and capability claims. Record transport failure separately from an agent-returned error. A 200 response with an error envelope fails the probe. The probe output may be shown as an observed smoke example, separate from both synthetic preview and evaluation scores.

No speculative retries for POST invocation. Default retry count is zero. Retrying after a timeout can duplicate real actions; enable it only when the target implements a tested idempotency contract or the user explicitly selected a repeatable test world. Retries are separate attempts and cannot replace the first authoritative evaluation attempt. Respect declared rate limits and bounded Retry-After handling without hiding the original failure.

### 7.2 Remote boundaries

The connector executes outside the agent sandbox in a restricted worker. Permit approved HTTPS endpoints; reject embedded URL credentials, non-HTTP schemes, loopback/link-local/cloud metadata/control-plane addresses and unapproved private networks. Validate all resolved IPv4/IPv6 addresses and the actual connected destination; disable redirects by default. Private endpoints require an administrator-created exact destination policy and a worker with reachability, never a chat-level “allow everything.” Keep metadata/control-plane denies even for private integration. These controls follow the [OWASP SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

Only the worker obtains the configured credential. Credential rotation invalidates verification, is audited and cannot rewrite an old manifest. Secrets are encrypted at rest under an install-owned key outside source control. Logs, exceptions, traces and exports are redacted before persistence. Header allowlists and origin checks prevent forwarding an API key to another endpoint.

For stateful remote APIs, each case/repeat requires its own remote session and declared initialization/reset/cleanup contract. R1 supports stateless JSON targets; detect declared statefulness and stop until an adapter implements reset. Local fresh containers cannot reset a remote CRM/database either; both target types must explicitly choose fixture isolation or test-resource lifecycle. Cleanup failures become evidence and visible warnings, not silent success.

### 7.3 What “live” means

| Mode | What is actually connected | Dashboard behavior |
|---|---|---|
| Synthetic preview | Versioned mock generator | Sample data banner; no evaluation result |
| Verified target | One bounded smoke invocation | Connection status and observed example; no quality claim |
| On-demand evaluation | Dataset invocations through a target worker | Live progress and measured partial results via platform SSE |
| Recurring synthetic evaluation, R2 | Schedule creates new frozen measured runs | Trends across runs; last/next run and stale status |
| Passive production monitoring, future | Separate authenticated event intake and sampling/consent policy | Production observations, with missing-label and sampling limits |

A remote output-only target cannot support a hard dollar cap on internal model usage. Enforce request count, concurrency, wall time and platform judge/harness spending; mark remote cost unknown or self-reported. Cancellation stops further dispatch and closes client requests, but does not prove a remote side effect stopped. Record `remote_execution_unknown` for in-flight requests unless the target's cancel contract acknowledges completion.

R2 connectors add server-sent token streams and asynchronous job submission/polling/cancellation. Streaming maps first byte, first token and final answer separately; malformed frames and disconnects yield partial evidence, never a completed answer. Async job IDs are persisted before polling; restart resumes polling rather than submitting another job. Both require protocol conformance fixtures before claiming support.

## 8. Local execution and trace integrity

The current subprocess runner remains an explicitly opt-in **trusted development** tool during migration. It is not a fallback for failed sandbox preflight and must never be selected automatically for uploaded code.

Implement v3 §11A/§11B/§12A: immutable source snapshot → restricted build job → digest-pinned image → fresh non-root container per case/repeat/attempt → canonical input/output protocol. No expected labels in the sandbox. Use read-only rootfs, bounded tmpfs, CPU/memory/PID/time caps, no host filesystem mount, no engine socket inside an agent, internal network only, restricted DNS and proxy-only outbound traffic. Worker control of a dedicated rootless engine socket is privileged platform capability, not agent capability.

The per-run proxy brokers real provider credentials while the sandbox receives dummy credentials. Generate a TLS-interception CA per installation and never commit its private key. Preserve application protocol fidelity in request/response streaming while decoding supported provider traffic into canonical events. LiteLLM SDK is the outbound model abstraction; LiteLLM Proxy server is not the intercepting forward proxy. [LiteLLM's documentation distinguishes the SDK from its API gateway](https://docs.litellm.ai/).

An explicit proxy environment variable alone is not enforcement: test direct IP, alternate DNS, IPv6, ignored proxy variables, redirects and unsupported protocols. The rootless/VM networking topology is a release-gating integration experiment; do not claim zero-change capture until its conformance tests pass on each platform. Certificate pinning, mTLS, opaque provider protocols or unsupported SDK behavior produce an explicit incompatibility with a documented supported adapter path. Never disable TLS checks to make a smoke test green. Generic HTTPS observations do not automatically reveal in-process tool execution.

Before dispatching a billable provider call, atomically reserve a conservative upper cost bound using a pinned price table and enforceable provider output cap. Include concurrent reservations in the run budget. Settle against returned usage; unavailable usage remains reserved/unknown and blocks further unsafe spending. Unknown pricing or unbounded provider billing cannot promise a hard USD ceiling; reject that configuration when a hard budget is required. Keep platform harness, judge and agent costs separate.

Re-redact adapter/remote enrichment before persistence; `redaction_state` supplied by the agent is not proof of redaction. Bind source identity at ingress and prevent forged `proxy` provenance. Preserve truncated/partial trace markers; safety evaluators cannot infer absence from incomplete capture. Local non-HTTP tools require tested adapter enrichment. Adapter events remain eligible for trace operators unless the metric explicitly filters them.

Tri-platform packaging uses Linux containers: native rootless Podman on Linux, Podman Machine on macOS, rootless Podman within WSL2 on Windows for the reference path. Keep the previously locked Docker Desktop/macOS restriction as a **project support policy**. The Compose client is compatible with a Podman API socket; host-facing and in-VM worker socket paths differ and must be detected rather than hard-coded. [Podman documents the socket and Compose integration](https://docs.podman.io/en/latest/markdown/podman-system-service.1.html) and [rootful/rootless Machine configuration](https://docs.podman.io/en/stable/markdown/podman-machine-set.1.html).

Tier 2 adds gVisor on Linux; Tier 3 microVM/public multi-tenant hosting remains future. “Compose up” is the startup command after runtime installation and preflight; no documentation may imply it installs Podman/WSL2 or creates a working VM by itself.

## 9. Evaluation semantics and use-case coverage

Keep v3 §7A, §9A, §11C, §13A, §15A and §21 as scoring authority. A metric separately declares target/selector, applicability, evaluator, per-case scoring, aggregation, `on_missing`, `on_error`, coverage and gate. Both missing/error choices are explicit; do not introduce hidden defaults through templates or run-scoped exceptions. Any current schema exception needs an explicit migration decision before R1.

Replace the handwritten scalar parser with pinned CEL plus a bounded environment and expression resource limits. CEL is a compile/check/evaluate language; the model authors a JSON field holding a CEL expression, not Python or a custom trace DSL. Trace sequencing remains closed structured JSON operators. [CEL's documented lifecycle is compile once and evaluate repeatedly](https://cel.dev/overview/cel-overview?hl=en).

For example, “every refund must have prior eligibility” is insufficient alone: no refund makes it vacuously true. Define applicability to refund-intended cases, a required action/completion metric, the ordering rule, and a coverage gate. With 20 applicable cases, 16 passing, 2 failing, 1 evaluator error and 1 missing required action, an explicit fail-on-error/fail-on-missing policy yields 16/20 = 80%. Excluding errors would change the denominator and must be visible. Zero eligible cases yields **not evaluated**, never 100%.

Per-case results retain PASS/FAIL/SKIP/ERROR and raw values with evidence. Unavailable capabilities are a validation/coverage state, not an invented numeric result. Show scored/eligible/total counts, applicability exclusions, missing outputs, errors and provisional scores. An enforceable gate is pass/fail only when eligibility, coverage, comparability and evaluator readiness suffice; otherwise return non-passing `insufficient_evidence` or `incomparable` to automation.

| ID | Use case and example | Evaluator / evidence | Release and limitation |
|---|---|---|---|
| U01 | Golden-answer accuracy | Exact/reference match or calibrated rubric; output plus approved labels | R1; inferred answers cannot silently become gold |
| U02 | Structured outputs / business rules | Schema and CEL/numeric predicates; typed output and case context | R1; malformed output distinct from transport error |
| U03 | Tool selection, arguments, result | Trace rules and schema/reference checks; executed tool events | R1 with supported adapter; unavailable for output-only API |
| U04 | Workflow ordering / approvals | Causal trace operators; tool and approval events | R1 for recorded workflow invariants; scripted interactive sessions R2 |
| U05 | Latency, errors and task completion | Connector/runtime timing and normalized outcome | R1; external latency includes network; never equate it with model time |
| U06 | Cost / tokens / cost per success | Proxy ledger and versioned price table | R1 local supported providers; unknown/self-reported remotely; zero successes is undefined, not zero cost |
| U07 | Regression between two versions | Paired case comparison and declared effect threshold/confidence interval | R1; incompatible versions/labels/worlds reported explicitly |
| U08 | Variance and intermittent failure | Independent repeats; pass frequency and interval | R1; retries do not replace first attempt |
| U09 | Semantic quality / tone / relevance | Versioned rubric, fixed judge model, output/label evidence | R1 optional; uncalibrated visible but excluded from gate enforcement |
| U10 | Multi-agent delegation | Delegation decision and attributed executed tool/spans | R1 supported CrewAI enrichment; no inference of sub-agent execution from a final answer |
| U11 | RAG citation/recall/context quality | Retrieval IDs, ranked chunks, gold relevant IDs, citation checks | R2 specialized metrics; final-answer checks already work R1 |
| U12 | Multi-turn conversation | Scripted turns, isolated session, per-turn and whole-dialogue result | R2; fixed scripts before stochastic user simulator |
| U13 | Security/prompt-injection behavior | Seeded hostile inputs, forbidden-action checks, refusal rubric | R1 fixture cases with available output/tool evaluators; advanced campaign generation deferred |
| U14 | Agent PII/secret echo | Synthetic canaries and capture-time detection flags | R1; platform redaction must not erase the fact that an echo occurred |
| U15 | Resilience to tool/provider faults | Versioned proxy faults/cassettes; ordering/output metrics | R1 local deterministic fault scenarios; cannot inject inside arbitrary remote services |
| U16 | Termination / loops / tool parsimony | Runtime limits and observed event counts | R1 local; remote internal loop detection unavailable without events |
| U17 | Streaming quality / first token | Token-stream adapter and timing markers | R2; first byte is not automatically first token |
| U18 | Re-score and dispute | Stored evidence, new evaluator revision and non-destructive human annotation | R1; expired required evidence blocks re-score |
| U19 | CI evaluation / release gate | Authenticated run API, immutable manifest, comparison and exit status | R1; CI configuration UI stays deferred |
| U20 | Recurring synthetic checks | Persisted schedules producing bounded measured runs | R2; no fabricated production traffic |
| U21 | Pairwise preference | Position-swapped judge, same cases, paired outputs | Preserve v3 judge capability; dedicated preference UI remains deferred |
| U22 | Production replay | Anonymized recorded input/world/cassette import with labels/provenance | Cassette mechanism R1; production monitoring bridge/replay product path deferred |
| U23 | Different frameworks/languages | Generic invocation or HTTP contract plus capability negotiation | R1 Python generic + CrewAI enrichment and language-independent HTTP; language-specific local build profiles expanded only after tests |
| U24 | Sharing/export and portfolio | Workspace project list, frozen reports and versioned downloads | R1; no public hosting or cross-workspace benchmark ranking |
| U25 | Domain-specific custom evaluator | Explicitly supplied, versioned evaluator artifact with typed input/output, executed in a separate restricted sandbox | R1 advanced operator path retained from v3; never LLM-emitted code, never in the API/worker or agent container; SDK/marketplace still deferred |

For canary tests, the capture boundary computes a narrowly defined match flag in memory before replacing the canary with a redacted value. Persist detector version, match category and evidence pointer, not raw sensitive content. Use only synthetic test secrets; never send real secrets merely to test whether an agent leaks them. Detector absence produces insufficient evidence.

Judge readiness remains `UNCALIBRATED`/`CALIBRATING`/`CALIBRATED` per v3. Drift returns the binding to `UNCALIBRATED` or `CALIBRATING`; it is not a fourth readiness state. Calibration labels have provenance and disagreement handling; the guided first ten labels bootstrap a set rather than proving universal reliability. The existing defaults require at least 30 labels and κ ≥ 0.6 for calibration, with κ < 0.5 triggering loss of readiness. Freeze model/rubric/parameters/cache key and do not silently fallback to another judge provider. Human overrides remain alongside machine results. Harness authoring model selection and evaluation judge selection are independent.

Custom evaluator artifacts are supplied by an authorized operator, digest-pinned and reviewed as ordinary code. Feed only the redacted selected evidence and necessary reference values to a fresh evaluator sandbox with no network, provider keys or agent filesystem. Bound runtime/output and validate the returned score schema; crashes yield evaluator ERROR under the metric's explicit policy. The conversational assistant can select an installed evaluator or describe an unsupported requirement, but cannot generate or install evaluator code during authoring.

## 10. Dashboard skill, artifacts and safe reuse

Ship a reviewed built-in skill under `skills/dashboard-authoring/SKILL.md`, a schema and approved template/component assets. The skill specifies input objects, allowed components, binding grammar, synthetic-data rules, readability/accessibility constraints and output validation. Uploading a repository containing a SKILL.md never installs or executes that file as a platform skill.

The skill returns a canonical DashboardDefinition, not HTML. Only validated registry components run. Generate preview and real results using the same server-side resolver/component library, switching their data provider. Existing lowercase prototype components require an explicit migration to canonical registry identities; do not maintain two vocabularies indefinitely. Rejected bindings leave the prior valid definition available.

Artifact layout on the persistent volume (logical keys, not user-supplied filesystem paths):

```text
workspaces/{workspace_id}/projects/{project_id}/
  sources/{source_version_id}/manifest.json
  knowledge/{knowledge_version_id}/understanding.json
  knowledge/{knowledge_version_id}/understanding.md
  evaluations/{evaluation_version_id}/spec.json
  datasets/{dataset_version_id}/cases.jsonl
  dashboards/{dashboard_version_id}/definition.json
  dashboards/{dashboard_version_id}/preview-data.json
  dashboards/{dashboard_version_id}/preview.html
  dashboards/{dashboard_version_id}/manifest.json
  runs/{run_id}/manifest.json
  exports/{export_id}/manifest.json
  exports/{export_id}/report.html
  exports/{export_id}/metrics.csv
  exports/{export_id}/results.json
```

Database rows remain authority for identity/state. The artifact manifest includes content hashes, all producing versions, creation time and whether data is synthetic or measured. Writes use staging + atomic rename or immutable object-store upload, followed by a transaction attaching successful artifacts. Orphan staging is collected; a DB pointer must not claim an artifact is ready before its checksum is verified.

Derived HTML contains escaped text and reviewed local assets only; no agent credential, active connection token, raw source bundle or network-dependent third-party asset. A static export keeps visible preview/measured/provisional labels and links to embedded retained evidence where included. It is a snapshot, not a silently reconnecting dashboard. Download authorization is workspace-scoped; filenames are generated by the platform.

## 11. APIs and background jobs

Extend the current API through typed resource routes; preserve existing v3 run APIs where possible. The table defines required behavior, not a demand to create duplicate aliases. During migration, old `/api/harness/chat` and `/api/evals` become compatibility façades into these services.

| Operation | Contract |
|---|---|
| `POST /api/projects/{id}/imports` | Source kind/ref or completed upload ID → 202 import job; invalid input never maps to sample |
| `GET /api/jobs/{id}` | Observed stage, counts, safe diagnostics and recovery action |
| `GET /api/projects/{id}/knowledge` | Current version plus confirmed/inferred/unknown facts |
| `POST /api/projects/{id}/knowledge/versions` | Reviewed changes + expected revision → immutable knowledge version |
| `POST /api/projects/{id}/sessions` | Durable authoring session and version pointers |
| `POST /api/sessions/{id}/turns` | Message/card action + expected revision + idempotency key → typed persisted turn/job |
| `POST /api/projects/{id}/datasets/imports` | File ID + field mapping → validation report and draft dataset version |
| `POST /api/evaluations/{id}/versions` | Canonical spec + knowledge/dataset refs → validation report/version |
| `POST /api/projects/{id}/connections` | Typed remote config + secret ref → draft connection version |
| `POST /api/targets/{id}/verify` | Exact target version + safe smoke input + authorization → verification job |
| `POST /api/dashboards/{id}/versions` | Canonical definition + expected revision → validated dashboard version |
| `POST /api/dashboard-versions/{id}/preview` | Immutable spec/dataset refs → deterministic preview artifacts |
| `POST /api/run-plans` | Version refs + limits → normalized manifest, readiness, estimates, authorization requirement |
| `POST /api/runs` | Manifest + bound authorization + idempotency key → queued run only after checks |
| Existing run detail/progress/cancel/resume/re-score routes | Same versioned state and evidence across both execution target types |
| `POST /api/comparisons` | Two frozen run/revision IDs + policy → comparability and statistical result |
| `POST /api/exports` | Frozen version refs + approved field selection → export job/download |
| `POST /api/schedules`, R2 | Frozen template or declared version-resolution policy, timezone, limits and authorization → persistent schedule |

Use one error envelope: `code`, `message`, `field_errors`, `retryable`, `operation_id`, `recovery_action`. Never include secrets or raw provider bodies. Mutation responses return actual state. Same idempotency key/same content returns original outcome; same key/different content returns conflict. Tenant is derived from authenticated server context.

Jobs use atomic enqueue/outbox, leases, heartbeat, fencing token, bounded retry and terminal diagnostics. Worker restart must not let a stale lease write a second result. Outbox/SSE messages have monotonic persisted IDs; reconnect uses Last-Event-ID with a snapshot fallback when history expired. Progress is at-least-once and clients de-duplicate. Exactly-once remote side effects are not promised without a target idempotency contract.

## 12. Data quality, comparison, retention and operations

Dataset import limits for R1: 25 MiB upload, 5,000 rows per run, 256 KiB per case input, explicit UTF-8 handling; larger collections may be stored in multiple versioned datasets. ZIP limits: 100 MiB compressed, 500 MiB extracted, 10,000 entries, 50 MiB per member, maximum 100:1 expansion ratio and nesting depth 20. Enforce while streaming, not after allocation. Reject traversal, absolute/drive/UNC paths, symlinks/hardlinks, special files, Unicode/case-fold collisions and encrypted archives. Apply source acquisition caps to Git too. These are configurable administrator ceilings; raising them is not a chat action. Preserve stricter v3 trace caps where specified.

The archive limits above are deliberately stricter initial product defaults than v3's 2 GiB extracted / 512 MiB per-member maxima, to keep a local installation's import work bounded. This is a recorded operational-default change, not removal of v3's archive defenses. Preserve the v3 R1 sizing envelope of up to five repeats per case (25,000 first attempts at the 5,000-case limit); retries and all in-flight work must also fit explicit attempt, time and spend ceilings.

Case identity separates stable `case_id`, input hash and reference/metadata versions. Comparisons require matching workflow/task semantics, metric/evaluator/judge binding, fixture world, applicable cases and reference versions. Report full cohort and matched subset sizes. A changed answer key requires re-scoring both preserved runs under the same new labels before comparable accuracy claims. Source version changes are the usual comparison variable; simultaneous model/tool/world changes are disclosed confounders.

Use v3's repeat-aware statistical method; paired estimates resample case-level repeat groups rather than treating correlated repeats as independent cases. Display effect size, uncertainty, baseline/candidate counts and declared material-regression threshold. Preserve §11C.4: 10,000 seeded bootstrap resamples, 95% intervals and at least three repeats for an interval; Quick runs may use point-value absolute gates with an explicit `no_ci` marker. A regression gate requiring an interval returns insufficient evidence when it cannot be computed. Do not mix smoke calls or synthetic preview into trends. A moving “latest run” filter must disclose which immutable run/revision it resolves to.

Use PostgreSQL RLS with non-owner application roles, transactional workspace context and explicit insert/update checks. Admin maintenance uses a separate narrowly controlled role. Test pooled-connection reuse and artifact/SSE access, not just query helpers. Table owners and BYPASSRLS roles can bypass policies, so schema presence alone is insufficient. [PostgreSQL documents these exceptions](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).

R1 local account roles: owner (credentials/settings), editor (drafts/approved runs) and viewer (results/exports permitted by policy). Bootstrap an install-owned owner credential; require authentication for non-loopback exposure and secure sessions/CSRF protection for browser mutations. Do not ship a shared default database/application password. CLI/CI tokens are scoped and revocable. Single-tenant deployment does not authorize arbitrary workspace crossover.

Keep v3 §18 retention and evidence-snapshot rules. Add preview retention (last 20 unreferenced revisions, 30 days), verification diagnostics (30 days), export artifacts (7 days default) and abandoned imports (24 hours). Referenced source/spec/dataset/dashboard manifests remain with their run. Evidence expiry is explicit; a score never silently loses all supporting evidence. Deleting a project tombstones active access, cancels future jobs and follows an auditable purge policy; it does not mutate already downloaded exports.

Back up PostgreSQL and artifacts consistently with a version manifest; back up install-owned encryption material separately under operator control. Verify restore resolves report/evidence links. Use per-workspace queue fairness, upload/storage quotas, redacted operational logs and optional local metrics. No telemetry, phone-home or automatic sharing of source data. Show an installation model-provider setting explaining which selected external provider receives the redacted analysis context. This is self-hosted storage, not a claim that cloud model calls stay on the machine.

Performance acceptance is measured on a documented reference machine: UI acknowledges commands within one second; no unexplained spinner beyond ten seconds; show staged import/build progress; resume persisted state after refresh. Dashboard reads use aggregate tables and paginated evidence, not full trace scans. Record measured p50/p95 for a 200-case fixture workload before setting release latency claims; cold build and remote provider latency remain visible contributors.

## 13. Recovery scenarios and release acceptance

| ID | Scenario | Required outcome |
|---|---|---|
| E01 | Missing repo/path or invalid Git ref | Explicit import failure; no sample substitution or fabricated tools |
| E02 | Malicious/oversized ZIP | Rejected before execution or unbounded extraction; cleanup and readable reason |
| E03 | Broken reference-agent syntax | Parse diagnostic with location; discussion/hosted mode allowed; local build blocked |
| E04 | No entrypoint or dependencies | Guided declaration/manifest recovery; actual smoke needed after build |
| E05 | Wrong SDK model string | Runtime misconfiguration, not sandbox failure; preserve provider evidence redacted |
| E06 | Unknown framework/tool visibility | Generic invocation where supported; capability gap visible and incompatible metrics blocked |
| E07 | User corrects understanding | New version with prior facts/evidence preserved; affected drafts become review-needed |
| E08 | Reload or two tabs edit | Exact persisted session recovered; stale edit conflict, no lost update |
| E09 | Invalid model-produced JSON or dashboard | Bounded repair then actionable validation error; prior valid draft remains |
| E10 | Labels missing/inferred | Accuracy coverage shows gap; health metrics may run; no invented gold labels |
| E11 | Preview edit then real run | Same approved dashboard definition and bindings render measured results |
| E12 | Hosted 401/403, 200 error body, wrong mapping | Distinct verification failures; none becomes “connected and ready” |
| E13 | Hosted redirect/DNS rebinding/private metadata | Request blocked; credentials never reach unapproved destination |
| E14 | Hosted timeout after possible action | First attempt remains uncertain/failure; no automatic duplicate mutation |
| E15 | Remote API provides no trace/cost | Output/timing score normally; internal metrics unavailable, cost unknown |
| E16 | Remote deployment changes mid-run | Mark affected run non-reproducible/incomparable; stop strict-version run |
| E17 | Stateful case leaks into next | Contract test fails; isolate/reset or reject target mode |
| E18 | Local agent ignores proxy variables | Direct egress fails; no inherited real credentials; no unsafe fallback |
| E19 | Agent forges proxy/cost/redaction event | Ingress rejects authority claim; trusted boundary controls provenance/redaction |
| E20 | Concurrent calls approach hard budget | Atomic reservation prevents over-dispatch; unknown spend blocks further calls |
| E21 | Agent loops/crashes / worker dies | Enforce limits, preserve partial evidence, mark incomplete, reap orphan containers |
| E22 | Disconnect during progress | SSE resumes by cursor or snapshot; no duplicate cases/runs |
| E23 | No action on a “never forbidden action” metric | Separate task-completion/applicability/coverage prevents vacuous success |
| E24 | Judge uncalibrated or drifted | Scores labeled provisional; excluded from enforceable gate |
| E25 | Different datasets/judge versions | Comparison explains incompatibility or reports explicitly limited matched subset |
| E26 | Re-score old run | New score revision; zero agent invocations; expired evidence blocks honestly |
| E27 | Export with malicious CSV/HTML content | Formula-injection-safe CSV; escaped HTML; no secrets; frozen versions retained |
| E28 | Cross-workspace row/artifact/stream request | Denied by auth/RLS/artifact policy, even with guessed valid IDs |
| E29 | Clean installation on each platform | Approved runtime/socket detected, fresh local run succeeds, boundaries tested |
| E30 | Cancel approved run during dispatch | No new cases dispatched; local orphan cleanup; remote uncertainty explicit |
| E31 | Backup and restore | Session/version/run/report links recover and hashes verify |
| E32 | Repeat pass after initial failure | Original authoritative attempt retained; flaky behavior visible |

### 13.1 Reference-agent walkthrough

1. Import `fixtures/reference-agent/multi_agent_system.py` unchanged. Detect the truncated syntax before dependency installation. Report static evidence honestly; do not call it smoke-ready.
2. Keep a reference-only knowledge draft, with CrewAI/Gemini and dynamic tools marked according to evidence. User may connect a separately deployed API even while this reference does not parse; source alignment is unknown/asserted, not verified.
3. For the local demonstration, create a separate repaired/versioned fixture with disclosed repairs: complete the missing tail, explicit kickoff entrypoint, SDK-correct model name and seeded tool fixtures. Do not overwrite the original.
4. Build/smoke the repaired copy through rootless sandbox/proxy. Confirm actual provider configuration and runtime tool registrations. The existing `fixtures/sample-agent` is a different support-triage fixture; it is not the promised repaired CrewAI example.
5. Select delegation correctness, required eligibility action and valid tool arguments. The proxy observes provider/network evidence; the tested CrewAI adapter supplies in-process execution detail. Causal rules include adapter events; they do not filter them out by default.
6. Preview uses synthetic samples. A measured quick run links each score to attempt/evidence, records versioned cost and shows framework coverage gaps. Repeats with seeded fixtures separate agent variance from unseeded mocks.
7. Re-score retained evidence under a revised metric, then compare two compatible repaired versions. Export results with source/repair disclosure and evidence provenance.

### 13.2 Definition of a complete R1 release

All E01–E32 scenarios pass where applicable to R1; R2-only protocols are explicitly unsupported rather than simulated. A new user can complete both a real stateless hosted-API evaluation and a real isolated local evaluation, reopen the project, edit metrics/dashboard, rerun, compare and export without editing source code or database rows. A custom metric survives arbitrary wording through the validated authoring pipeline. Nontechnical users can review and run an already configured target without entering JSON. Every success/readiness indicator corresponds to observed backend state.

## 14. Scope and implementation sequence

**R1 — complete core workflow:** truthful status; authenticated persistence/RLS/artifacts; Git/ZIP ingestion; confirmed knowledge; durable chat; metric selection/custom intent; dataset/golden review; skill-driven preview and saved revisions; hosted synchronous JSON target; local sandbox/proxy and supported adapters; deterministic/optional judge scoring; partial results; evidence; regression comparison; re-score; CSV/JSON/HTML export; API/CI gating; tested tri-platform startup. This is larger than a demo MVP and should not be described as a one-week build.

**R2 — protocol and evaluation expansion:** remote SSE/async/session adapters, scripted multi-turn/approval interactions, richer RAG metrics, recurring synthetic schedules, additional auth/connectors/framework profiles. Each ships only after its evidence and isolation contracts are tested.

**Still deferred by prior decisions:** passive production monitoring/replay workflow, dedicated pairwise preference UI, error-path coverage inference, evaluator marketplace/SDK, dedicated LangGraph/OpenAI Agents adapters, CI configuration UI, full human review queues, dashboard plugins, embeddings-based clustering and public multi-tenant execution. Existing cassettes, judge capability, generic adapters and notifications keep these possible. No deferred capability is implied by a dashboard placeholder.

Follow the [implementation checklist](workflow-implementation-plan.md) in dependency order: first remove false success and establish durable contracts; then secure source/connection boundaries; then authoring and preview; then real execution/evidence; finally comparison/export/release hardening. Carry the existing passing tests through the migration and add contract/acceptance tests for missing behavior. Required feasibility proofs may change a library choice, but cannot silently weaken a locked guarantee.

No unresolved product question prevents starting this plan. External secrets, private endpoints, platform test machines and unsupported protocol details remain real integration inputs; an implementation agent must report precisely which capability they block while continuing unrelated tasks.
