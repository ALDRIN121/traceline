# R1 and R2 End-to-End Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development when available) to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Implement and verify every R1 requirement and the explicitly defined R2 extensions in the governing design documents.

**Architecture:** Preserve the modular control-plane monolith and durable worker model. Add immutable run plans and authorization, route execution through workers, use rootless Podman plus a per-run recording proxy for local agents, and share one normalized result/scoring/evidence pipeline across hosted and local adapters. Add R2 protocols as adapters rather than architectural forks.

**Tech Stack:** Python 3.11+, FastAPI/Pydantic, PostgreSQL with RLS, SQLite development storage, rootless Podman, LiteLLM SDK, CEL, browser tests with Playwright, and the existing locked dependencies.

**Spec:** `docs/superpowers/specs/2026-09-18-r1-r2-end-to-end-design.md`, `docs/design/LLM_Agent_Evaluation_Engine_v3.md`, `docs/design/end-to-end-workflow-plan.md`, `docs/design/workflow-implementation-plan.md`

## Global Constraints

- Preserve the locked v3 invariants and document any implementation-level divergence.
- Never execute uploaded source, a custom evaluator, or a build step as a trusted host subprocess.
- Keep the broken reference fixture unchanged and add repaired examples separately.
- Use explicit typed states and fail closed for unsupported protocols, missing evidence, unknown usage, stale authorization, and unavailable runtime controls.
- Use tests-first for every behavior change; run the focused test red, implement the minimum behavior, then run it green before refactoring.
- Keep all persisted resources workspace-scoped and use the non-owner PostgreSQL application role in integration tests.
- Keep UI changes in the frontend specialist path and verify live API behavior separately from browser rendering.

---

### Task 1: Run plans, authorization, and evaluation worker admission

**Files:**
- Create: `src/llm_agent_eval/run_plans.py`
- Create: `src/llm_agent_eval/authorization.py`
- Modify: `src/llm_agent_eval/contracts.py`, `src/llm_agent_eval/storage.py`, `migrations/0007_runs.sql`, `src/llm_agent_eval/workflow_api.py`, `src/llm_agent_eval/worker.py`
- Test: `tests/workflow/test_run_plans.py`, `tests/workflow/test_target_runs.py`, `tests/integration/test_worker_resume.py`

**Interfaces:**
- `plan_run(version_refs: dict[str, str], limits: dict[str, object], actor: Actor) -> RunPlan`
- `authorize(plan_hash: str, actor: Actor, policy: AuthorizationPolicy) -> Authorization`
- `enqueue_run(plan_hash: str, authorization_id: str, idempotency_key: str, actor: Actor) -> JobRecord`
- `ExecutionJobHandler(actor, command, context) -> dict`

- [ ] Write failing tests for frozen references, stale target verification, mismatched authorization hash, duplicate submission, workspace denial, and durable `evaluation_run` dispatch.
- [ ] Run the focused tests and confirm they fail because the run-plan objects, routes, and worker handler do not exist.
- [ ] Add immutable run-plan and authorization records with content digests, readiness blockers, capability requirements, estimates, limits, and execution policy.
- [ ] Add API routes for run-plan creation, authorization, queued run submission, and job status using the uniform error envelope.
- [ ] Add the worker handler boundary without executing agents yet; it must reject source versions whose runtime readiness is not executable.
- [ ] Run the focused workflow and PostgreSQL tests, then the existing run lifecycle tests.

### Task 2: Restricted builds and rootless local target execution

**Files:**
- Create: `src/llm_agent_eval/runtime/build.py`, `src/llm_agent_eval/targets/local.py`
- Modify: `src/llm_agent_eval/runtime/podman.py`, `src/llm_agent_eval/runtime/sandbox.py`, `src/llm_agent_eval/runner.py`, `src/llm_agent_eval/engine.py`, `src/llm_agent_eval/worker.py`
- Test: `tests/runtime/test_build.py`, `tests/integration/test_local_target.py`, `tests/integration/test_case_isolation.py`

**Interfaces:**
- `BuildService.prepare(source_version: SourceVersion, runtime_profile: RuntimeProfile) -> BuildJob`
- `LocalTargetAdapter.verify(target_version, smoke_input, execution_context) -> VerificationRecord`
- `LocalTargetAdapter.invoke(invocation_manifest, case_input, execution_context) -> InvocationResult`
- `LocalTargetAdapter.cancel(invocation_id, execution_context) -> CancellationResult`

- [ ] Write failing tests for digest-pinned build admission, source/image mismatch, no host execution, per-case fresh containers, missing entrypoint, invalid model configuration, cancellation, and orphan reaping.
- [ ] Implement restricted build provenance and cache keys covering source, lockfile, base digest, adapter, and build-policy versions.
- [ ] Replace uploaded-agent host invocation with `LocalTargetAdapter` through the durable worker.
- [ ] Extend the sandbox command vector for controlled proxy networking while retaining read-only roots, bounded tmpfs, dropped capabilities, and empty environment defaults.
- [ ] Implement the v3 input/manifest/output protocol and six smoke-failure classifications.
- [ ] Add marker-leak and host-file/environment/socket access fixtures; verify the second case cannot observe the first.
- [ ] Run focused runtime tests and the full local run lifecycle suite.

### Task 3: Proxy topology, CA trust, provider transport, and authoritative capture

**Files:**
- Create: `src/llm_agent_eval/egress/listener.py`, `src/llm_agent_eval/egress/transport.py`, `src/llm_agent_eval/egress/decoders.py`, `src/llm_agent_eval/egress/cassettes.py`
- Modify: `src/llm_agent_eval/egress/ca.py`, `src/llm_agent_eval/egress/recording.py`, `src/llm_agent_eval/capture.py`, `src/llm_agent_eval/events.py`, `src/llm_agent_eval/engine.py`, `src/llm_agent_eval/runtime/sandbox.py`
- Test: `tests/integration/test_proxy_capture.py`, `tests/integration/test_budget.py`, `tests/integration/test_egress_bypass.py`, `tests/integration/test_cassettes.py`

**Interfaces:**
- `ProxyInstance.start(run_manifest, route_table, budget) -> ProxyEndpoint`
- `ProxyInstance.stop() -> CleanupRecord`
- `ProviderTransport.send(route, request, secret_ref) -> ProviderResponse`
- `CassetteWorld.exchange(fingerprint, request, mode) -> ReplayResult`

- [ ] Write failing live-topology tests for proxy-only HTTP, direct IPv4/IPv6, alternate DNS, redirect, IP-literal TLS, ignored proxy variables, proxy restart, and cleanup.
- [ ] Connect the existing install CA to per-run leaf certificates and inject only the certificate into the sandbox trust store.
- [ ] Implement the listener and per-run network so the agent cannot route around it.
- [ ] Implement bounded OpenAI/Anthropic/Google request/response/stream decoders and explicit generic HTTP fallback failures.
- [ ] Broker secrets only at the outbound leg, stamp trusted provider/model/price identity, and reject adapter overrides.
- [ ] Connect reservation/settlement/uncertain budget behavior to concurrent requests.
- [ ] Persist redacted events and evidence caps before storage; reject forged authority claims.
- [ ] Implement record/replay/hybrid cassette modes with versioned fingerprints, fault injection, and zero billing for replay.
- [ ] Run the proxy integration suite on the supported reference runtimes.

### Task 4: Shared hosted/local run scoring and judge wiring

**Files:**
- Create: `src/llm_agent_eval/scoring_service.py`, `src/llm_agent_eval/calibration.py`
- Modify: `src/llm_agent_eval/engine.py`, `src/llm_agent_eval/worker.py`, `src/llm_agent_eval/profiles.py`, `src/llm_agent_eval/rubric_store.py`, `src/llm_agent_eval/judge_gateway.py`, `src/llm_agent_eval/targets/http_json.py`, `src/llm_agent_eval/storage.py`, `src/llm_agent_eval/workflow_api.py`
- Test: `tests/integration/test_target_runs.py`, `tests/workflow/test_judge_run_wiring.py`, `tests/workflow/test_calibration.py`

- [ ] Write failing tests for one shared attempt/result/scoring path, remote output-only evidence, unknown remote cost, deterministic score persistence, judge profile selection, rubric/context resolution, and provisional gates.
- [ ] Implement the target adapter registry and route hosted verification/invocation through durable execution jobs.
- [ ] Add output evidence records that do not fabricate trace files for hosted targets.
- [ ] Wire persisted profiles and rubrics into `GatewayJudge` with exact attempt-bound context resolvers.
- [ ] Separate deterministic scoring and judge scoring jobs while preserving first-attempt authority, repeats, retries, partial states, and evidence references.
- [ ] Implement calibration state transitions and gate eligibility.
- [ ] Run hosted/local shared-path integration tests and existing evaluator tests.

### Task 5: Comparison, dispute, re-score, exports, and CI API

**Files:**
- Create: `src/llm_agent_eval/comparisons.py`, `src/llm_agent_eval/exports.py`, `src/llm_agent_eval/ci_api.py`
- Modify: `src/llm_agent_eval/storage.py`, `src/llm_agent_eval/api.py`, `src/llm_agent_eval/cli.py`, `src/llm_agent_eval/dashboard.py`
- Test: `tests/workflow/test_comparisons.py`, `tests/workflow/test_rescore.py`, `tests/workflow/test_exports.py`, `tests/test_ci_api.py`

- [ ] Write failing tests for comparable and incomparable runs, paired bootstrap, no-CI behavior, dispute lineage, evidence-only re-score, immutable export snapshots, formula-safe CSV, escaped HTML, and CI exit codes.
- [ ] Implement comparability keys, case-key intersection, 10,000 seeded resamples, confidence intervals, material-regression gates, and confounder disclosure.
- [ ] Implement non-destructive human overrides and judge dispute calibration updates.
- [ ] Implement export jobs with frozen manifests, hashes, retained evidence, partial/provisional labels, and secret exclusion.
- [ ] Add authenticated CI run/status/comparison endpoints and webhook-triggered evaluation submission.
- [ ] Verify duplicate run submission cannot dispatch or spend twice.

### Task 6: R1 live UI and release operations

**Files:**
- Modify: `web/index.html`, `web/app.js`, `web/authoring.js`, `web/styles.css`
- Create: `tests/browser/execution/execution.spec.js`, `tests/browser/evidence/evidence.spec.js`
- Modify: `docker-compose.yml`, `.github/workflows/release-checks.yml`, `README.md`, `docs/design/workflow-implementation-status.md`
- Test: live API browser tests, `tests/integration/test_restore.py`, `tests/integration/test_release_paths.py`

- [ ] Add failing browser tests for live project bootstrap, dataset/label review, preview revision, manifest authorization, hosted/local execution, SSE reconnect, evidence inspection, comparison, re-score, and export.
- [ ] Implement the UI through the required frontend specialist path; keep browser controls nontechnical and state-driven.
- [ ] Add retention, quotas, staging cleanup, backup/restore, install-key procedure, health checks, and worker/runtime separation.
- [ ] Run E01–E32 applicable R1 acceptance scenarios, clean install, Compose startup, PostgreSQL RLS, browser, and cross-platform runtime checks.
- [ ] Update the status document only from observed evidence and preserve explicit limitations.

### Task 7: R2 streaming and asynchronous targets

**Files:**
- Create: `src/llm_agent_eval/targets/http_stream.py`, `src/llm_agent_eval/targets/http_job.py`, `src/llm_agent_eval/streaming.py`
- Modify: `src/llm_agent_eval/contracts.py`, `src/llm_agent_eval/worker.py`, `src/llm_agent_eval/workflow_api.py`
- Test: `tests/workflow/test_stream_target.py`, `tests/workflow/test_async_target.py`, `tests/integration/test_stream_recovery.py`

- [ ] Write failing tests for bounded SSE parsing, first-byte/first-token/final timestamps, malformed frames, duplicate submission, remote job ID persistence, restart polling, timeout, disconnect, and cancellation uncertainty.
- [ ] Implement typed stream and remote-job invocation results with bounded frames/bodies and no implicit retries.
- [ ] Persist remote IDs before polling and resume existing jobs after worker restart.
- [ ] Add capability declarations and reject metrics incompatible with missing stream/final evidence.

### Task 8: R2 stateful and interactive evaluation

**Files:**
- Create: `src/llm_agent_eval/targets/sessions.py`, `src/llm_agent_eval/interactions.py`
- Modify: `src/llm_agent_eval/datasets.py`, `src/llm_agent_eval/worker.py`, `src/llm_agent_eval/engine.py`
- Test: `tests/workflow/test_stateful_target.py`, `tests/integration/test_state_reset.py`

- [ ] Write failing tests for isolated sessions, initialize/reset/cleanup, scripted user turns, approval pauses, turn/conversation metrics, and state leakage.
- [ ] Implement explicit session contracts and fresh session identity per case/repeat.
- [ ] Enforce approval before side effects and classify disconnect/cancellation uncertainty.
- [ ] Add stateful capability metadata to manifests and comparison keys.

### Task 9: R2 RAG and additional tested adapters

**Files:**
- Create: `src/llm_agent_eval/evaluators/retrieval.py`, `src/llm_agent_eval/targets/frameworks.py`
- Modify: `src/llm_agent_eval/evaluators.py`, `src/llm_agent_eval/knowledge.py`, `src/llm_agent_eval/contracts.py`
- Test: `tests/workflow/test_retrieval_metrics.py`, `tests/workflow/test_adapter_matrix.py`

- [ ] Write failing tests for ranked retrieval recall@k, citation correctness, missing retrieval evidence, adapter capability matrices, and unsupported framework classification.
- [ ] Implement retrieval evidence envelopes and deterministic hand-checked metrics.
- [ ] Add only adapters with explicit version/conformance tests; unsupported frameworks remain honest failures.

### Task 10: R2 recurring synthetic evaluation

**Files:**
- Create: `src/llm_agent_eval/schedules.py`
- Modify: `src/llm_agent_eval/storage.py`, `src/llm_agent_eval/workflow_api.py`, `src/llm_agent_eval/worker.py`, `src/llm_agent_eval/api.py`
- Test: `tests/workflow/test_schedules.py`, `tests/integration/test_schedule_recovery.py`

- [ ] Write failing tests for timezone/DST due slots, missed-run policy, persistent locks, idempotency, overlap prevention, daily request/USD limits, frozen/new-version policy, and stale-verification pause.
- [ ] Implement schedule records, due-slot calculation, lock ownership, enqueue idempotency, and bounded notification state.
- [ ] Add schedule routes and worker sweeper behavior without auto-running beyond authorization.

### Task 11: Final requirement audit and release evidence

**Files:**
- Modify: `docs/design/workflow-implementation-status.md`, `README.md`, `docs/design/end-to-end-workflow-plan.md`
- Test: all offline, PostgreSQL, browser, live-runtime, and release suites

- [ ] Run the complete requirement matrix against FR-01–FR-19, E01–E32, UX-01–UX-22, R1 tasks T00–T19, and R2 tasks T20–T23.
- [ ] Verify every named artifact, command, route, lifecycle state, invariant, and platform claim against direct evidence.
- [ ] Remove obsolete prototype claims only when the replacement path is actually verified.
- [ ] Mark the goal complete only after no required item remains incomplete or weakly evidenced.
