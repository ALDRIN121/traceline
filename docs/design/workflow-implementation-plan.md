# Traceline Complete Workflow Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to execute this plan task by task. Delegate all interface design and implementation to `frontend-engineer`, as AGENTS.md requires. Use other subagents only when separately authorized. Checkboxes are completion records, not suggestions to skip dependencies.

**Goal:** Deliver the confirmed-understanding → metrics → dataset → saved preview → hosted/local verification → measured run → explanation/comparison/export workflow for real agents.

**Architecture:** Extend the tested engine behind durable versioned authoring services and two execution adapters. Keep a single declarative dashboard renderer, a trusted scoring/evidence boundary, and PostgreSQL-backed jobs and workspace isolation.

**Tech stack:** Existing Python ≥3.11, FastAPI/Pydantic, PostgreSQL, browser client; add pinned LiteLLM SDK, CEL implementation, rootless Podman execution and recording proxy per the governing design. The repository already has a Python toolchain. New dependencies and browser-test tooling must be installed and declared before their commands are advertised as available.

**Spec:** [End-to-end plan](end-to-end-workflow-plan.md), [UI/UX contract](workflow-ui-ux-plan.md), and [v3](LLM_Agent_Evaluation_Engine_v3.md), with [the original review](v2-review-and-v3-plan.md) governing earlier mechanisms.

## Global constraints

- Apache-2.0; self-hosted; no telemetry, phone-home or bundled credentials.
- `workspace_id` and PostgreSQL RLS on every table; shipping deployment is single-tenant.
- Proxy-only sandbox egress, per-install CA, dummy agent keys, credentials and cost owned by proxy.
- LiteLLM **SDK**, not LiteLLM Proxy server, is the outbound model abstraction.
- No agent framework in the harness; LLM outputs validated JSON; scalar predicates use CEL; trace operators remain closed JSON.
- `on_missing` and `on_error` are explicit, without hidden defaults; scoring, aggregation and gate are separate.
- One fresh sandbox per case/repeat/attempt; retries do not replace authoritative first attempts.
- Adapter events are first-class evidence; ingress binds authority and re-redacts them.
- Harness conversations and evaluated-agent traces remain separate.
- One canonical dashboard definition/renderer for preview and measured results; preview is unmistakably synthetic.
- Preserve existing user edits and the original broken reference fixture. Do not rewrite the repository as a shortcut.
- R1 includes synchronous stateless hosted JSON APIs and isolated local execution. R2 protocols must not be presented as shipping R1 behavior.

## 1. Execution rules and checkpoints

Read all three new documents once, then load only the relevant v3 sections per task. Before implementation, inspect current source and working changes: line anchors in the audit describe September 8, not a permanent API. Do not discard work another agent/user has completed since that review. If a task is already implemented, demonstrate its acceptance criteria and record it as verified rather than rebuilding it.

The existing baseline at planning time was `PYTHONPATH=src python -m pytest -q`: 513 passed, 3 skipped, 3 deselected, 2 warnings. Keep a baseline run at execution start. No live/provider or external mutation is needed to draft this plan. Use stub servers/cassettes for contract tests; use real environments for sandbox/platform acceptance. Skipped tests never count as evidence for the corresponding release requirement.

Each task follows this cycle:

1. Inspect the named code and relevant spec; write failing behavior tests for the stated cases.
2. Run the focused tests and record the expected failure, distinguishing missing functionality from a broken fixture.
3. Implement the smallest complete service/route/interaction slice; preserve compatibility deliberately.
4. Run focused checks, plus affected existing tests and required integration/browser checks.
5. Update the task checkbox and evidence log only after observed success. Make a scoped checkpoint commit if the worktree policy permits; never sweep unrelated changes into it.

At execution time create `docs/design/workflow-implementation-status.md` with one row per task: status (`pending`, `in_progress`, `verified`, `blocked`), changed files, checks/results, artifact links, unresolved blocker and next action. Record HEAD and manifest/dependency versions. A task is complete only when its exit criteria are observed; source code existing or a tool returning “queued” is insufficient.

**Agent-sized decomposition:** Tasks below are independently reviewable increments, not promises of five-minute implementation. Each contains explicit substeps and contract specimens. If one spans multiple agent turns, checkpoint within it, keep its checkbox open and resume from the evidence log. Do not skip security/network proofs to satisfy a “single shot” request.

## 2. File and module ownership map

The following new paths are **planned**, not present merely because they appear here. Keep the existing package while separating new responsibilities. If a current module already implements the exact responsibility, adapt it and record the mapping instead of duplicating it.

| Path | Responsibility |
|---|---|
| `src/llm_agent_eval/contracts.py` | Version envelopes, typed command/error/result/capability objects |
| `src/llm_agent_eval/auth.py` | Workspace/user/token context and role checks |
| `src/llm_agent_eval/versions.py` | Immutable version creation and optimistic concurrency |
| `src/llm_agent_eval/artifacts.py` | Scoped immutable artifact writes/reads/manifests/GC |
| `src/llm_agent_eval/jobs.py` | PostgreSQL leases, outbox, fencing, retry and cancellation |
| `src/llm_agent_eval/secrets.py` | Install-owned encryption, references/rotation, authorized secret access |
| `src/llm_agent_eval/redaction.py` | Trusted redaction/canary detection for all persistence boundaries |
| `src/llm_agent_eval/ingestion.py` | Bounded Git/ZIP acquisition and immutable source snapshots |
| `src/llm_agent_eval/discovery.py` | Parsing, evidence-backed facts, confidence and affected-source invalidation |
| `src/llm_agent_eval/knowledge.py` | Confirmed knowledge versions, retrieval and readable report generation |
| `src/llm_agent_eval/sessions.py` | Durable authoring turns, state/version refs and confirmation records |
| `src/llm_agent_eval/authoring.py` | Structured proposals and bounded validation/repair, using existing harness/gateway |
| `src/llm_agent_eval/datasets.py` | Import/mapping, case/label provenance and immutable dataset versions |
| `src/llm_agent_eval/previews.py` | Deterministic data provider, artifact bundles, review receipts |
| `src/llm_agent_eval/targets/base.py` | Shared verification/invocation/cancellation interface |
| `src/llm_agent_eval/targets/http_json.py` | R1 remote endpoint adapter and typed mapper |
| `src/llm_agent_eval/targets/network_policy.py` | DNS/IP/TLS/redirect/origin policy |
| `src/llm_agent_eval/runtime/build.py` | Restricted build job and digest-pinned image cache |
| `src/llm_agent_eval/runtime/podman.py` | Sandbox lifecycle, network preflight and orphan cleanup |
| `src/llm_agent_eval/runtime/custom_evaluator.py` | Separate digest-pinned evaluator sandbox with bounded typed I/O and no network |
| `src/llm_agent_eval/proxy/` | Separate interception, credential, budget, decoder and fixture modules |
| `src/llm_agent_eval/adapters/crewai.py` | Version-tested in-process evidence enrichment, no secrets |
| `src/llm_agent_eval/comparisons.py` | Comparability, matched cohort, paired estimates and regression outcomes |
| `src/llm_agent_eval/exports.py` | Frozen CSV/JSON/HTML result bundles |
| `src/llm_agent_eval/operations.py` | Backup/restore, retention, quotas, health and readiness |
| `migrations/` | Versioned PostgreSQL DDL and reversible/forward migration policy |
| `skills/dashboard-authoring/` | Reviewed SKILL.md, schema references and renderer template assets |
| `web/` | Frontend-specialist-owned modular extension of current shell and renderer |
| `tests/workflow/` | End-to-end service contract acceptance, external services stubbed |
| `tests/integration/` | Real PostgreSQL, sandbox/proxy, provider-protocol and OS checks |
| `tests/browser/` | Real browser user-flow/accessibility checks and visual evidence |
| `fixtures/repaired-reference-agent/` | Separate disclosed/versioned repaired CrewAI example |

Keep `spec.py`, `evaluators.py`, `trace_rules.py`, `events.py`, `judge.py`, `engine.py`, `dashboard.py`, `storage.py`, `api.py`, `harness.py` and `gateway.py` as compatibility/semantic anchors while extracting relevant behavior. New routes live in focused modules and are registered by `create_app`; avoid adding another thousand lines to `api.py`.

## 3. Contract-test harness used by the specimens

The snippets below specify intended behavior for implementation; **they were not executed during planning**. Their fixture is implemented in T02 at `tests/workflow/conftest.py`, backed by a disposable PostgreSQL database, a temporary artifact root, deterministic fake ModelGateway and in-process/stub HTTP services. No existing `.env`, production endpoint or real database is used.

`platform` has this deliberately small interface, implemented in `tests/workflow/driver.py`:

```python
class WorkflowDriver:
    def seed(self, scenario: str) -> dict: ...
    def post(self, path: str, body: dict, *, key: str | None = None,
             actor: str = "editor") -> dict: ...
    def get(self, path: str, *, actor: str = "editor") -> dict: ...
    def drain(self, job_id: str) -> dict: ...
    def artifact(self, artifact_id: str) -> bytes: ...
    def invocation_count(self, target_id: str) -> int: ...
```

These signatures define a test adapter, not production APIs. Return values from `post/get` are `{"http_status": int, "body": dict}`; `drain` returns the terminal job payload and fails the test on a bounded drain timeout. `seed` only creates the explicitly documented test scenarios in each task, using production repositories, not shortcut mutation of result rows. `invocation_count` reads the stub target's actual invocation log. The driver must not synthesize successful state. Real sandbox tests use actual Podman and an isolated stub upstream provider instead of a fake runtime.

Implement each task's scenario builder with its described input data and output IDs before its specimen. All fixture IDs and artifact names come from generated objects; never assume an existing run/database. Register `integration` and `browser` test markers in T00, keeping `live` opt-in. Once a file exists, focused unit/service command is `PYTHONPATH=src python -m pytest -q <test-file>`; integration commands additionally require their documented disposable services. Do not claim an uncreated command/test was run.

## 4. R1 tasks

### T00 — Establish reproducible baseline and truthful project guidance

**Dependencies:** none. **Files:** modify `AGENTS.md`, `README.md`, `pyproject.toml`, `Dockerfile`, `.env.example`; create status document and lockfiles using the chosen existing-compatible packaging workflow. Preserve the owner's current Docker/Compose edits while documenting limitations.

- [ ] Confirm current worktree and re-run the actual existing default tests. Record warnings and skipped/live scope.
- [ ] Replace stale “no code/no tests/v3 missing” guidance with verified paths/commands and links to this package. Keep the locked architectural invariants unchanged.
- [ ] Declare missing ZIP dependency `python-multipart`; resolve/pin a clean installation, runtime and dev dependencies. Never embed provider/database credentials in images or lockfiles.
- [ ] Test clean environment installation, CLI help and `create_app` with a temporary DB. Register future test markers without implying their tests exist.

**Exit:** clean install can construct API/ZIP routes; existing test baseline is accounted for; no files falsely claim the full sandbox/hosted workflow already ships. Tests: extend `tests/test_api.py` for app construction; add `tests/test_installation.py` for declared route dependencies/package smoke. Covers A19.

### T01 — Remove fabricated source, runtime and success claims

**Dependencies:** T00. **Files:** modify `orchestrator.py`, `api.py`, focused tests in `tests/test_api_extensions.py`; frontend specialist updates matching status rendering/tests.

**Interface:** analysis returns actual `source_id`, findings and `verification_id | null`; schema validation returns `validation_status`, never a smoke status. Unknown input is `source_not_found`; the example source is selected only by an explicit example action.

- [ ] Add tests: nonexistent source, unrelated minimal Python project and broken reference agent must never return sample tools/model or `smoke_passed: true`.
- [ ] Remove sample fallback and model/tool defaults from both analysis paths. Separate schema validation from actual engine smoke records; failed validators must not render completed/successful stages.
- [ ] Make null verification render “Not verified.” Replace simulated subagent thought text with concise observed tool summaries. Preserve error details safely.
- [ ] Exercise existing sample path explicitly and prove it still works without being the fallback for other sources.

**Contract example:**

```python
def test_unknown_source_never_becomes_sample(client):
    response = client.post("/api/projects/analyze", json={"source": "/missing/traceline-agent"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_not_found"
```

`client` is the app fixture already used in API tests; adapt its setup to the established test file. Expected initial failure is the current fallback 200. Covers A02/A06, E01/E03, UX-02/UX-09.

### T02 — Durable object versions, workspace authorization and artifact storage

**Dependencies:** T01. **Files:** create `contracts.py`, `auth.py`, `versions.py`, `artifacts.py`, `migrations/0001_workflow_objects.sql`, `tests/workflow/conftest.py`, `tests/workflow/driver.py`, `tests/workflow/test_versions.py`, `tests/integration/test_rls.py`; adapt storage/config/API.

**Interfaces:** `VersionStore.create(kind, parent_id, content, expected_revision, actor) -> VersionRecord`; `ArtifactStore.put(workspace_id, bytes, media_type) -> ArtifactRecord`; `ArtifactStore.get(workspace_id, artifact_id) -> bytes`. Typed records contain the fields specified in master §6. Workspace context comes from auth, not content.

- [ ] Add non-owner application roles, per-table RLS with USING/WITH CHECK and transaction-scoped tenant context. Include new/existing objects and no-table bypass tests under pooled reuse.
- [ ] Implement immutable versions, parent/digest identity, active pointer and conflict behavior. Migrate existing eval/spec/dashboard records without dropping old runs; quarantine invalid legacy definitions with a reason.
- [ ] Implement staged artifact upload/checksum/atomic attachment and scoped downloads. Deny cross-workspace artifacts and guessed IDs; do not expose arbitrary filesystem paths.
- [ ] Implement test harness above and scenario `two_workspace_projects`: two owners/editors, isolated projects, known readable knowledge revision in each.
- [ ] Verify optimistic concurrency, RLS, artifacts and rollback after failed artifact write; no empty object falsely marked ready.

```python
def test_stale_edit_is_conflict(platform):
    s = platform.seed("two_workspace_projects")
    body = {"expected_revision": 0, "facts": [], "confirmed": False}
    path = f"/api/projects/{s['project_id']}/knowledge/versions"
    assert platform.post(path, body)["http_status"] == 201
    assert platform.post(path, body)["http_status"] == 409
```

**Exit:** E08/E28; a process restart recovers versions; a different actor/workspace cannot read or write them. No UI work depends on a mutable JSON blob as authority afterward.

### T03 — Durable jobs, idempotency, secret references and redaction

**Dependencies:** T02. **Files:** `jobs.py`, `secrets.py`, `redaction.py`, `migrations/0002_jobs_secrets.sql`, `tests/workflow/test_jobs.py`, `tests/integration/test_job_recovery.py`, `tests/test_redaction.py`.

**Interfaces:** `JobQueue.enqueue(command, idempotency_key) -> JobRecord`; `claim(worker_id) -> LeasedJob`; `complete(job_id, fence, result)` rejects stale fence. `SecretStore.resolve(secret_ref, authorized_service)` returns a value only in process memory. `redact(payload, policy) -> RedactedPayload` contains safe content, redaction version and detector flags.

- [ ] Implement transactional outbox/queue, leases, heartbeat/fencing, bounded retry and one terminal transition. Same key/content returns same job; changed content returns 409.
- [ ] Generate install encryption material outside repo, store encrypted secrets and typed refs, enforce service/role access and audited rotation. Refuse shared bundled defaults.
- [ ] Apply trusted redaction before chat, model error, import log, remote payload or trace persistence. Test nested headers/strings, exception messages, canary flags and oversized content.
- [ ] Kill/restart workers during claim and output attachment. Prove stale worker cannot commit and artifacts remain consistent. Cancellation persists intent before worker acknowledgment.

**Scenario/test:** enqueue a deterministic artifact job twice with one key; assert one artifact hash and one completed command; expire first worker lease, reclaim, and assert late completion from old fencing token is rejected. Put sentinel `sk-test-do-not-persist-0001` in all input channels and assert it is absent from DB/artifacts/log captures. Covers E19/E21/E22/E28.

### T04 — Secure immutable Git and ZIP ingestion

**Dependencies:** T03. **Files:** `ingestion.py`, import routes, `tests/workflow/test_imports.py`, `tests/integration/test_archive_limits.py`, `tests/fixtures/imports/` with generated archive fixtures.

**Interface:** `ImportSource(kind, upload_id | repo_url/ref, credential_ref)` → import job → immutable SourceVersion/diagnostics. Expose master import/job routes and one upload route returning a scoped upload ID.

- [x] Stream uploads and reject master §12 limit violations before extraction growth. Reject traversal/drive/UNC paths, links, special files, duplicate/case/Unicode collisions, encrypted ZIPs and expansion bombs.
- [x] Fetch approved HTTPS Git source in an isolated acquisition worker; resolve ref to commit, cap checkout, disable hooks/submodule/LFS implicit execution and redact credential transport. Persist source digest and acquisition manifest.
- [x] Quarantine raw imports with restricted encrypted storage. Produce the approved sanitized snapshot, exclude credential files from analysis/build/export, scan allowed text before model context, and retain masked exclusion/provenance records. Test hardcoded-secret and `.env` sentinels never reach the model or readable report.
- [x] Parse supported syntax before dependency install. Preserve parse-failed source/reference report with blocked local readiness. Cleanup partial files on cancel and failure.
- [x] Add scenarios `valid_zip`, `missing_git_ref`, `reference_broken_zip`, `zip_traversal`, `zip_case_collision`, `zip_expansion_limit`; each yields upload/repo input and expected typed error where applicable.

```python
def test_zip_traversal_rejected(platform):
    s = platform.seed("zip_traversal")
    r = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"])
    job = platform.drain(r["body"]["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "unsafe_archive_path"
    assert job.get("source_version_id") is None
```

**Exit:** E01–E04; unrelated agent import contains no sample-domain defaults; no import runs user code.

### T05 — Evidence-backed discovery, confirmed knowledge and retrieval

**Dependencies:** T04. **Files:** `discovery.py`, `knowledge.py`, knowledge routes, `tests/workflow/test_knowledge.py`; fixtures for nested repo and dynamic tool registration.

**Interfaces:** `discover(SourceVersion) -> DiscoveryReport`; `KnowledgeStore.confirm(report_id, corrections, expected_revision) -> KnowledgeVersion`; `retrieve(project_id, version_id, query, budget) -> cited snippets`. Use existing ModelGateway only for bounded explanation, never static truth.

- [ ] Parse nested supported files, infer workflow/entrypoint/framework/model/tool candidates with actual file locations. Dynamic registration remains low-confidence until runtime evidence.
- [ ] Write structured and Markdown understanding reports; persist confirmation/corrections/unknowns and source dependencies. Read source as data and ignore embedded tool instructions.
- [ ] Add source-update diff/invalidation, bounded lexical retrieval and confirmed summary caching. If adding v3 vector retrieval, use the pinned v3-supported pgvector version, with workspace filtering verified in SQL; do not make embeddings required for basic onboarding.
- [ ] Scenario `dynamic_tool_repo`: a runtime dict names three tools, no entrypoint, no invocation. Confirm one business fact, then update source to remove it and show review-needed status.

```python
def test_static_findings_do_not_claim_execution(platform):
    s = platform.seed("dynamic_tool_repo")
    report = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    assert report["verification_id"] is None
    assert all(f.get("observed_in_attempt_id") is None for f in report["facts"])
    assert report["needs_entrypoint_declaration"] is True
```

**Exit:** E06/E07; report and exact pending question survive restart. Covers A02/A03 and UX-01/UX-05.

### T06 — CEL and evaluator compatibility migration

**Dependencies:** T02; can proceed after T05 in this sequential plan. **Files:** adapt `predicates.py`, `spec.py`, `evaluators.py`, `trace_rules.py`; add `tests/test_cel_contract.py`, migration fixtures.

**Interface:** retain public predicate compile/evaluate façade where feasible; underlying implementation is pinned `cel-python` from v3, selected version verified against required environment. `language_version` identifies old prototype versus CEL rules. Do not silently reinterpret stored rules.

- [ ] Encode v3 variables/type/missing/error semantics as a conformance corpus, including aliases, explicit null versus missing, cost/timestamps, and bounded expression resource use.
- [ ] Replace handwritten parser with CEL. Preserve trace JSON operator structure and adapter evidence eligibility. Add explicit `on_missing` for every metric contract path and visible migration errors for older exemptions.
- [ ] Revalidate legacy stored specs; migrate equivalent expressions mechanically only where equivalence is tested. Quarantine non-equivalent rules and leave old score revisions readable.
- [ ] Prove vacuous safety cannot bypass completion/coverage gates, and evaluator errors follow explicit denominator policy.

**Tests:** a CEL map/index operation unsupported by the old parser compiles and evaluates as the declared v3 environment requires; unknown identifiers fail at validation; missing required target emits configured missing outcome; `for_all` includes adapter tool events; 16 pass/2 fail/1 error/1 missing produces 80% under fail/fail policy. Run existing predicate, trace, evaluator and spec tests after intentional migration changes. Covers A13/A14, E23/E32.

### T07 — Stateful harness, model gateway and typed metric proposals

**Dependencies:** T03, T05, T06. **Files:** `sessions.py`, `authoring.py`, adapt `harness.py`, `gateway.py`, `orchestrator.py`, chat routes; `tests/workflow/test_sessions.py`, `tests/test_gateway.py`.

**Interfaces:** `process_turn(session_id, message | card_action, expected_revision, operation_id) -> TurnResult`; result references proposed version IDs and observed tool states. `ModelGateway` uses pinned LiteLLM SDK and structured-output validation, with existing fake gateway retained for tests.

- [x] Save sessions/messages/tool calls and version pointers. Load confirmed reference + selected eval context before responding; no keyword-only replacement of current evaluation.
- [x] Implement metric suggestions with stable IDs, required capabilities, label needs and rationale. Multi-selection is one proposal; custom wording follows the same compiler/validator path.
- [x] Apply bounded repair, provenance checks and resource budgets. Stop after two failed repairs with the prior draft intact; do not fabricate labels or successful execution.
- [x] Separate harness/judge model bindings, preserve structured usage/provenance and provider errors. Selecting unavailable model credentials does not disable reading results or deterministic runs.
- [x] Scenario `authored_session`: confirmed support workflow, three candidate metrics, no verified target. Second turn “make latency 2 seconds” must patch that eval's metric and leave its other metrics unchanged.

```python
def test_turn_changes_current_evaluation(platform):
    s = platform.seed("authored_session")
    r = platform.post(f"/api/sessions/{s['session_id']}/turns", {
        "message": "Make the latency limit two seconds",
        "expected_revision": s["revision"]}, key="latency-edit")
    turn = platform.drain(r["body"]["job_id"])
    assert turn["evaluation_id"] == s["evaluation_id"]
    assert turn["state"] == "validated"
    assert turn["verification_id"] is None
```

**Exit:** E08/E09; arbitrary wording tests use deterministic gateway responses and verify applied artifacts, not a canned answer string. Covers A03–A05/A15.

### T08 — Dataset mapping, labels, provenance and selection

**Dependencies:** T05–T07. **Files:** `datasets.py`, dataset routes; `tests/workflow/test_datasets.py`, `tests/fixtures/datasets/`.

**Interface:** `validate_import(upload_id, mapping, metric_requirements) -> DatasetValidationReport`; `commit_dataset(report_id, explicit_exclusions, expected_revision) -> DatasetVersion`. Input, reference, metadata hashes are separate.

- [x] Implement CSV/JSON/JSONL streaming parsing, master limits, null/missing distinctions, deterministic JSON Pointer/column mapping and row error reports.
- [x] Store case IDs, input hashes, labels/provenance, tags/splits, required fixture state. Reject contradictory/duplicate identities and disclose deliberate exclusions.
- [x] Review/confirm generated cases; case labels from the evaluated agent remain unapproved until independently confirmed. Golden data cannot enter the agent-visible request.
- [x] Scenario `mixed_label_dataset`: 4 cases, two approved labels, one inferred label, one missing label. Health metrics eligible on four; enforceable accuracy eligible on two with coverage report 2/4.

```python
def test_inferred_is_not_gold(platform):
    s = platform.seed("mixed_label_dataset")
    report = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    assert report["label_coverage"]["approved"] == 2
    assert report["label_coverage"]["total"] == 4
    assert report["cases"][2]["label_status"] == "inferred"
```

The read route above is the matching version-detail resource for the dataset service. **Exit:** E10/E23, UX-08; all excluded rows accounted for and versions immutable.

### T09 — Canonical dashboard skill, deterministic preview and saved artifacts

**Dependencies:** T02/T07/T08. **Files:** `previews.py`, `dashboard.py`, `skills/dashboard-authoring/SKILL.md`, reviewed assets/schema refs, preview routes; `tests/workflow/test_previews.py`, `tests/test_dashboard.py`. Frontend specialist owns any template/rendering code.

**Interface:** `build_preview(dashboard_version, evaluation_version, dataset_version, generator_version) -> PreviewManifest`; use canonical DashboardDefinition/bindings and existing resolver, injecting a synthetic data provider.

- [x] Migrate chat's incomplete dashboard shape into canonical registry definitions; reject unknown components/bindings instead of dropping them. Persist a migration/version map for existing evals.
- [x] Write the built-in skill: only schema JSON, approved registry, type-checked bindings, no embedded scripts/remote assets. Repo-provided skills cannot alter platform instructions.
- [x] Implement deterministic preview generation from declared ranges/expectations, seeded by spec hash; no undeclared numbers/verdicts. Render placeholder where unavailable.
- [x] Atomically save definition/data/manifest/derived preview.html; revision edits and undo create immutable versions. Review receipt is presentation-only and bound to exact revision context.
- [x] Scenario `declared_range_dashboard`: one latency metric range, one range-less metric, known definition and dataset. Generate twice; hashes identical; range-less metric remains placeholder.

```python
def test_preview_is_reproducible(platform):
    s = platform.seed("declared_range_dashboard")
    path = f"/api/dashboard-versions/{s['dashboard_version_id']}/preview"
    a = platform.drain(platform.post(path, s["refs"])["body"]["job_id"])
    b = platform.drain(platform.post(path, s["refs"])["body"]["job_id"])
    assert a["data_hash"] == b["data_hash"]
    assert b["data_kind"] == "synthetic"
    assert b["gate_result"] is None
```

**Exit:** E09/E11 and UX-07/UX-22. Same definition ID must later flow into T15 real results and T18 exports.

### T10 — Nontechnical authoring interface

**Dependencies:** T05/T07–T09. **Owner:** frontend-engineer exclusively for interface design/code. **Files:** specialist-selected modules under `web/`, browser tests under `tests/browser/authoring/`; no main-agent UI edits.

- [ ] Implement project/import/report pages and chat + artifact panel using the companion spec. Add durable session resumption and review/correction cards with actual statuses.
- [ ] Add multi-select metric cards, typed custom intent, explicit missing/error choices and advanced definition inspection. Field validation references actual backend errors.
- [ ] Add dataset mapping/label review and canonical preview revisions. Preserve selection and edits across refresh/errors/two-tab conflicts.
- [ ] Declare/install browser harness before using it. Implement UX-01–UX-08, UX-18–UX-21 with actual DOM interactions and test data from T02; inspect desktop/mobile screenshots and motion/preferences as required.

**Exit:** a keyboard-only user imports, confirms, selects/customizes metrics, reviews data and saves a preview; no automatic execution. Backend truth, preview banner and version IDs visible. A source-only audit is not sufficient acceptance.

### T11 — Hosted JSON target, network policy and real verification

**Dependencies:** T03/T06/T08. **Files:** target modules from §2, connection/verification routes, `tests/workflow/test_http_target.py`, `tests/integration/test_endpoint_policy.py`, `tests/fixtures/http_agent_server.py`.

**Shared interface (also used by T13):**

```python
class TargetAdapter:
    def verify(self, target_version, smoke_input, execution_context): ...
    def invoke(self, invocation_manifest, case_input, execution_context): ...
    def cancel(self, invocation_id, execution_context): ...
```

Return typed `VerificationRecord`, `InvocationResult` and `CancellationResult` respectively; definitions live in `contracts.py`. InvocationResult contains normalized output, outcome, connector/runtime observations, capability profile, evidence refs and remote uncertainty. The execution context is service-owned workspace/job/secret authorization; it is never LLM-authored.

- [x] Implement master JSON Pointer mapping and configured output schema. Verify none/bearer/API-key auth via encrypted refs; expected labels cannot be mapped.
- [x] Support bounded contract/OpenAPI import as schema assistance; resolve bundled references only, disable external `$ref` fetches, and require explicit operation/endpoint selection before verification.
- [x] Implement endpoint/IP/DNS/TLS/redirect policy with protected destinations and administrator-managed private allowlist. Pin the validated address at connection time while preserving TLS hostname verification.
- [x] Implement real smoke invocation with typed 401/403/network/200-error/mapping/size/timeout outcomes; persist exact version/secret refs, expiry and observed deployment identity. Do not count health GET alone as invocation success.
- [x] Implement stateless API invocation, external timing, request/concurrency limits, zero retries by default and unknown remote cost. Reject unsupported stateful/stream/async mode honestly.
- [x] Scenario `output_only_api`: local test server permitted by test-only exact policy; returns `{"answer":{"status":"ok"}}`, no telemetry, unique call log. Production policy still rejects arbitrary localhost.

```python
def test_remote_verification_does_not_invent_tool_evidence(platform):
    s = platform.seed("output_only_api")
    r = platform.post(f"/api/targets/{s['target_id']}/verify", s["smoke_request"])
    v = platform.drain(r["body"]["job_id"])
    assert v["state"] == "verified"
    assert v["capabilities"]["final_output"] == "observed"
    assert v["capabilities"]["tool_execution"] == "unavailable"
    assert v["capabilities"]["provider_cost"] == "unavailable"
```

**Exit:** E12–E17/E30, UX-03/UX-04/UX-09. DNS rebinding and credential redirect fixtures must pass before arbitrary user endpoints are enabled.

### T12 — Prove containment and proxy networking on real platforms

**Dependencies:** T03/T04. **Files:** `runtime/podman.py`, `proxy/interception.py`, `scripts/runtime_preflight.py`, `tests/integration/test_egress_boundary.py`, platform setup documentation under `docs/design/`.

This is a gating engineering experiment that becomes the tested runtime foundation. Do it before claiming uploaded agents can run. It may require Linux/macOS/WSL2 hosts; if one is unavailable, record that platform as unverified and keep its release gate open.

- [ ] Detect rootless runtime, engine/API version, VM/socket paths and capabilities. Select isolation by AGENT_RUNTIME without an alternate engine design.
- [ ] Create internal per-run network and proxy topology, read-only roots and bounded scratch. Prove agent has no engine socket, host-file route or real provider credential.
- [ ] Exercise ignored HTTP_PROXY, direct IPv4/IPv6, alternate DNS, UDP, redirect, IP-literal TLS, pinned certificates and proxy restart. Authorized supported HTTP reaches stub upstream; bypass attempts fail closed.
- [ ] Generate per-install CA and trust injection, prove different installs have different private keys and source/image contains no key. No host-wide certificate trust mutation is required for the application agent sandbox.
- [ ] Record packet/connection-level proof, exact runtime versions and cleanup behavior for all supported OS reference environments. Verify unsupported protocol returns classified failure instead of an insecure bypass.

**Exit:** E18/E29. If rootless network mechanism cannot meet the design, produce a concrete incompatibility report and alternative within the approved tier; do not continue with plain subprocess execution as if this task passed.

### T13 — Restricted builds and fresh-case local target adapter

**Dependencies:** T12. **Files:** `runtime/build.py`, `runtime/podman.py`, local target adapter, adapt `runner.py`/`engine.py`; `tests/integration/test_local_target.py`.

**Interface:** `BuildService.prepare(source_version, runtime_profile) -> BuildJob`; output contains image digest and dependency/resolved-config provenance. Local adapter implements T11 interface using v3 file protocol.

- [ ] Build in a separate restricted environment with bounded dependency egress, no application secrets, reviewed runtime profiles and digest-pinned bases. Cache key includes source, lockfile, base digest, adapter and build policy versions.
- [ ] Run every case/repeat/retry in a fresh sandbox. Agent-visible files include only input/manifest, no expected values; output validation/timeouts and resource limits preserve partial diagnostics.
- [ ] Integrate real DECLARE/PREPARE/SMOKE states and six v3 failure classifications. No trace may proceed only with declared output-only capability and compatible metrics.
- [ ] Add cancellation/orphan reaping and fenced job recovery. Hold any engine socket only in authorized runtime worker; no agent/container-build access.
- [ ] Implement the separate custom-evaluator sandbox retained by v3: operator-supplied digest-pinned artifact, redacted selected evidence/reference input, no network/secrets/shared agent state, bounded typed score output. Timeout/crash becomes evaluator ERROR. Test that a custom evaluator cannot reach the API, provider key or agent filesystem.
- [ ] Fixture writes a marker then next case reads it: second case must not see it. Another attempts environment/host-file access and fails. Missing entrypoint and invalid model configuration yield their distinct failures.

**Exit:** E03–E06/E17/E18/E21/E29/E30; current trusted subprocess runner is opt-in developer-only and never fallback for uploads.

### T14 — Authoritative capture, cost, cassettes and framework evidence

**Dependencies:** T12/T13/T03. **Files:** `proxy/credentials.py`, `proxy/budget.py`, `proxy/decoders.py`, `proxy/world.py`, `adapters/crewai.py`, adapt capture/events/ingest; `tests/integration/test_proxy_capture.py`, `tests/integration/test_budget.py`, repaired fixture and README.

**Interfaces:** proxy emits existing TraceEvent envelope with ingress-bound source/trust and payload refs; budget reservation has atomic reserve/settle/cancel operations; decoder versions are frozen in run manifest. The adapter sends enrichment to `/_enrich`; no socket/volume secret channel.

- [ ] Implement protocol-fidelity tests for initial OpenAI/Anthropic/Google supported request/response/stream fixtures and generic HTTP fallback; use LiteLLM outbound SDK only where mapping preserves supported semantics. Unsupported mapping fails explicitly.
- [ ] Broker credentials; stamp provider/model/price identity from trusted source. Reserve concurrent worst-case billable usage, settle actual usage and stop on unknown/unbounded spend under hard-budget policy.
- [ ] Redact at capture and trusted enrichment ingress, cap events/payloads and persist explicit truncation. Reject forged authority; adapter tool events remain usable with honest provenance.
- [ ] Implement deterministic cassette record/replay/mocks/fault injection, seed/version/key matching and secret-safe cassette data. Replay does not bill historical cost again.
- [ ] Create disclosed repaired CrewAI reference copy and version-tested adapter for runtime dict tools/delegation. Preserve the broken fixture byte-for-byte. Test tool call evidence versus mere proposed tool call in a model response.

**Budget acceptance:** 10 simultaneous calls, each with maximum reserved cost 0.02 USD, remaining budget 0.05 USD: at most two may dispatch before settlement. Fake/unknown usage cannot release reservations into more unbounded calls. **Capture acceptance:** sentinel credentials absent across persisted output; provider/cost IDs cannot be overridden by adapter payload. Covers E05/E06/E19/E20 and master reference walkthrough.

### T15 — Unified manifest, actual authorization and run/scoring lifecycle

**Dependencies:** T06/T08/T09/T11/T13/T14. **Files:** run-plan service/routes, adapt engine/lifecycle/API/storage; `tests/workflow/test_run_plans.py`, `tests/workflow/test_target_runs.py`, `tests/integration/test_worker_resume.py`.

**Interfaces:** `plan_run(version_refs, limits) -> RunPlan`; `authorize(plan_hash, actor, policy) -> Authorization`; `enqueue_run(plan_hash, authorization_id, idempotency_key) -> RunRecord`. Validate exact refs again at dispatch.

- [ ] Freeze all master §6.5 fields and expose capability/label/readiness blockers, estimates, repeat multiplier, quotas and side-effect policy. Cost estimates never override hard limits.
- [ ] Validate presentation receipt separately from execution authorization. Require matching hash and non-expired target verification. Same idempotent submission gives one run.
- [ ] Execute either target through shared attempt/result/scoring path; persist output evidence for remote output-only metrics without requiring a fabricated trace file. Separate execution and aggregation state.
- [ ] Decouple deterministic and judge scoring jobs; preserve retries/repeats, first-attempt authority, partial/error/coverage rollup, calibrated gate eligibility and re-score versioning.
- [ ] Persist aggregate updates/outbox/SSE IDs, reconnect snapshots, cancellation and restart uncertainty. Dashboard resolves the run's pinned approved definition, not a default replacement.
- [ ] Scenario `approved_http_run`: two golden cases, verified stub target, reviewed preview, authorization and frozen manifest. A second scenario changes dataset after authorization.

```python
def test_duplicate_run_submit_spends_once(platform):
    s = platform.seed("approved_http_run")
    a = platform.post("/api/runs", s["request"], key="run-one")
    b = platform.post("/api/runs", s["request"], key="run-one")
    assert a["body"]["run_id"] == b["body"]["run_id"]
    platform.drain(a["body"]["job_id"])
    assert platform.invocation_count(s["target_id"]) == 2
```

Count excludes setup smoke by resetting the fixture counter after verification. **Exit:** E08/E11/E15/E20–E24/E30/E32; both hosted and local full core paths produce genuine scored results.

### T16 — Connection, run review, live results and evidence interface

**Dependencies:** T10/T11/T15. **Owner:** frontend-engineer. **Files:** modules in `web/`, `tests/browser/execution/`, `tests/browser/evidence/`.

- [ ] Implement hosted form/mapping/secret control and local preparation progress with actual verification receipts and capability table. No general JSON/code requirement for typical configured targets.
- [ ] Review exact run manifest and authorize once; stale edits invalidate approval visibly. Buttons use native keyboard/release activation and stable idempotency keys.
- [ ] Show measured partial/final/unknown/provisional states, scoped cost, trace source, expected/actual and deep links. Keep selection/scroll while SSE updates. Preserve approved preview layout when results arrive.
- [ ] Verify UX-09–UX-14, UX-18–UX-22, plus mobile, keyboard, screen reader and theme/preferences combinations. A disconnected browser must recover authoritative state, not replay a run submission.

**Exit:** both R1 paths work through actual UI with recorded visual/interaction evidence and honest recovery. Browser screenshots alone do not verify the backend gate; backend contracts remain required.

### T17 — Comparison, judge calibration, disputes and re-score

**Dependencies:** T15. **Files:** `comparisons.py`, adapt judge/revision routes and storage; `tests/workflow/test_comparisons.py`, `tests/workflow/test_rescore.py`. Frontend specialist owns associated UX.

- [ ] Compare matched case/input/reference/world/evaluator/judge identities; disclose cohort and confounders. Use v3 seeded case-level paired bootstrap, 10,000 resamples, 95% confidence, minimum repeat rule and visible no-CI behavior.
- [ ] Preserve absolute Quick point gates versus CI-based regression gates. Missing evidence/incompatible bindings returns non-passing automation status rather than numeric pretend regression.
- [ ] Implement guided label/dispute revision lineage; `UNCALIBRATED → CALIBRATING → CALIBRATED` with exact v3 defaults and reset behavior. No cross-provider fallback or mixed-binding calibration cache.
- [ ] Re-score retained evidence under a new validated binding without invoking the agent; insufficient/expired evidence blocks. Human override is adjacent to machine result, never destructive replacement.
- [ ] Scenario `completed_http_run`: stored output evidence, original score revision, zeroed call counter, changed evaluator version. Another has changed label/reference version and must fail comparability until both are rescored.

```python
def test_rescore_does_not_invoke_agent(platform):
    s = platform.seed("completed_http_run")
    r = platform.post(s["rescore_path"], s["rescore_request"], key="rescore-one")
    out = platform.drain(r["body"]["job_id"])
    assert out["score_revision_id"] != s["original_score_revision_id"]
    assert platform.invocation_count(s["target_id"]) == 0
```

**Exit:** E24–E26/E32, UX-15/UX-16. Complete calibrated and provisional test paths without requiring real model billing.

### T18 — Frozen exports, evidence reports and CI API contract

**Dependencies:** T16/T17. **Files:** `exports.py`, export/comparison API, CLI result/gate support, reviewed report templates; `tests/workflow/test_exports.py`, `tests/test_cli.py`. Frontend specialist owns export controls/template appearance.

- [ ] Produce scoped CSV/JSON/HTML artifact bundles with manifest hashes, exact versions, filters/cohort, provenance, uncertainty and retained evidence. Labels and partial states survive export.
- [ ] Escape HTML and neutralize spreadsheet formula prefixes on every CSV text field; preserve raw values in JSON with ordinary safe JSON encoding. No connection token, raw secret or external tracking asset.
- [ ] Pin export snapshot before background generation, verify artifacts before download-ready state and test later score revisions do not change downloaded bytes.
- [ ] Provide documented authenticated CI run/status/comparison contract and stable CLI outcomes: 0 passing, 1 failed quality gate, 2 configuration/insufficient/incomparable/incomplete. Actual implementation may preserve an existing numeric convention only if documented and compatibility-tested.
- [ ] Scenario `partial_provisional_export`: filtered partial run with uncalibrated metric and formula-like case text. Assert report badges/denominators, frozen refs, protected CSV and zero secrets.

```python
def test_export_keeps_truth_labels(platform):
    s = platform.seed("partial_provisional_export")
    r = platform.post("/api/exports", s["request"])
    export = platform.drain(r["body"]["job_id"])
    report = platform.artifact(export["html_artifact_id"]).decode()
    assert "Partial results" in report
    assert "Provisional" in report
    assert s["secret_sentinel"] not in report
```

**Exit:** E27, UX-17/UX-22; the downloaded report can be understood outside the app without losing evidence limitations. CI tests exercise both gate failure and insufficient evidence.

### T19 — Operations, deployment and complete release rehearsal

**Dependencies:** all R1 tasks above. **Files:** `operations.py`, migration/backup/restore scripts, Dockerfile/Compose, `.env.example`, README/platform docs, `tests/integration/test_restore.py`, `tests/integration/test_release_paths.py`; status evidence.

- [ ] Compose starts authenticated API, workers, PostgreSQL and artifact volume with readiness checks and generated credentials; agent sandboxes/proxies remain separately managed. Pin images/dependencies; no privileged shortcut to pass health checks.
- [ ] Enforce retention/evidence snapshots, preview/export TTLs, quotas, orphan/staging cleanup and per-workspace fairness. Show expired evidence and disable unsupported re-score.
- [ ] Run consistent backup/restore including encryption-key operational procedure and verify all source/spec/dashboard/run/evidence hashes and links. Practice interrupted migration recovery without silently resetting existing runs.
- [ ] Execute E01–E32 and UX-01–UX-22 applicable to R1, including original/repaired reference walkthrough, unrelated agent and output-only remote agent. Test supported Linux, macOS and WSL2 configurations; record limitations as release blockers, not passing matrix rows.
- [ ] Run full existing+new offline suite once after final changes, required integration/browser suites, clean install and documented smoke. Inspect final diff and verify no accidental secrets, CA keys or original fixture edits. Provider-live checks use separately supplied authorization/credentials and remain distinct from offline proof.

**Exit:** the complete master R1 definition is met. Update completion report with actual checks and open limits. No “done” claim if the UI only demonstrates sample data, a hosted request lacks evidence semantics, or local uploads still execute in the app process.

## 5. R2 extension tasks, explicitly after R1

| Task | Dependencies and files | Concrete behavior and required proof |
|---|---|---|
| T20 — Streaming/async target protocols | T11/T15; `targets/http_stream.py`, `targets/http_job.py`, protocol fixtures | Parse bounded SSE frames with first-byte/first-token/final boundaries; persist remote job ID before polling; restart polls existing job; disconnect/cancel marks uncertainty; test duplicate submit and malformed streams. |
| T21 — Stateful and interactive evaluation | T20/T13; `targets/sessions.py`, `interactions.py`, dataset interaction schemas | Separate session per case/repeat; initialize/reset/cleanup contract; scripted user turns/approval pauses; metric turn/conversation scope; fixture proves no action before scripted approval and no state leakage. |
| T22 — RAG and additional tested adapters | T14/T21; `evaluators/retrieval.py` after package migration or focused `retrieval_metrics.py`, new adapter modules | Known ranked retrieval fixture yields hand-checked recall@k/citation checks; missing retrieval remains unavailable; exact framework/version conformance matrix; no unconditional “all frameworks supported.” |
| T23 — Recurring synthetic evaluation | T15/T17/T19; `schedules.py`, schedule routes/jobs, UI owned by specialist | Timezone/DST/missed-run policy; persistent lock/idempotency; overlap disabled; daily request/USD limits; frozen or explicit new-version policy; stale verification pauses; test one run per due slot and no auto-run beyond authorization. |

Each R2 task gets the same test-first/review/evidence cycle. Passive production monitoring, dedicated pairwise UI and the remaining accepted scope cuts do not enter these tasks accidentally. Advanced auth requires its own connector-specific contract and tests.

## 6. Coverage map and integration checkpoints

| Requirements | Tasks | Evidence |
|---|---|---|
| Truthful state / accurate import / existing-code alignment | T00/T01/T04/T05 | A01–A06; E01–E09; UX-01/02/05 |
| Persisted memory and iterative authoring | T02/T03/T05/T07/T10 | restart/concurrent-edit tests; UX-05/06/18 |
| Metrics, labels and scoring semantics | T06/T07/T08/T15/T17 | denominator, CEL, missing/error/repeat/calibration tests |
| Skill/HTML preview and saved edits | T09/T10/T16/T18 | identical definition through synthetic/real/export; UX-07/22 |
| Hosted API auth/verification/evidence | T11/T15/T16 | E12–E17; real stub invocation log and policy tests |
| Local containment/capture/budget/framework | T12/T13/T14 | network proof per OS; unchanged broken and separate repaired fixture |
| Streaming progress/recovery/cancellation | T03/T15/T16 | lease restart/SSE replay/cancel tests; E21/22/30 |
| Golden eval, regression and re-score | T08/T17 | matched labels/cases and zero agent invocations on re-score |
| Nontechnical UX/accessibility | T10/T16/T17/T18 | UX suite, screenshots and keyboard/screen-reader observations |
| Export/CI/operations/security | T02/T03/T18/T19 | E27–E31, clean install, restore and gate codes |
| Advanced scenarios / recurring synthetic runs | T20–T23 | protocol/session/retrieval/scheduler conformance after R1 |

Integration checkpoints:

- **C1, after T05:** unrelated source → evidence-backed confirmed report; restart recovers it. No execution claim.
- **C2, after T10:** confirmed report → selected/custom metric → reviewed dataset → persisted synthetic preview, all through UI.
- **C3, after T11:** real stateless API verification, honest capability profile; authoring can proceed independently of local runtime.
- **C4, after T14:** repaired CrewAI fixture truly runs under proxy-enforced rootless containment and generates scored-capable evidence.
- **C5, after T16:** both target types complete measured runs through UI using the approved definition.
- **C6, after T19:** compare/re-score/export/CI/restore and full platform release checks pass. Only this checkpoint supports “R1 complete.”

## 7. Copyable implementation-agent kickoff

Use this as the first message to an implementation agent after reviewing this documentation package:

> Implement Traceline R1 using `docs/design/end-to-end-workflow-plan.md`, `docs/design/workflow-ui-ux-plan.md`, and `docs/design/workflow-implementation-plan.md`. First read AGENTS.md, inspect the actual worktree and preserve existing changes. The repository has implementation code: do not assume the stale design-only description is still accurate. Read the governing v3/review sections relevant to each task.
>
> Execute T00–T19 in dependency order, continuing through the complete authorized workflow. Use the existing passing code as a foundation and replace prototype shortcuts explicitly. Delegate every UI decision and UI code change to frontend-engineer under the repository's house design instructions. Keep other work sequential unless separately authorized to delegate. Create and maintain `docs/design/workflow-implementation-status.md` with observed verification evidence and next actions so another run can resume without repeating completed work.
>
> For each task, write meaningful failing contract tests, implement the slice, run focused checks and required integration/browser validation, then checkpoint only that work. Never claim smoke/connection/run success without a persisted observed result. Never use sample fallback, generated evidence, self-reported redaction/cost, arbitrary LLM frontend code or an unsandboxed upload runner to make the workflow appear complete. Preserve the original broken reference fixture and create a disclosed repaired copy for the real demonstration.
>
> Complete both stateless hosted API and rootless local sandbox paths, durable chat/knowledge, selectable/custom metrics, datasets/golden review, one skill-guided declarative preview/results renderer, saved revisions, real execution/evidence, regression comparison, re-score and exports. R2 remains deferred. If a real external dependency blocks a check, state the exact blocker, continue independent tasks, and leave that release gate unverified. Finish with files changed, actual tests and scenarios passed, remaining limitations and precise next steps; do not substitute a demo for the complete R1 release contract.

This handoff authorizes implementation work when the owner gives it to the implementation agent. Creating this plan itself did not run external agents, contact user endpoints, alter deployment credentials, or start implementation.
