# Workflow implementation status

This record separates verified prototype behavior from the v3 target design. It is updated as
workflow-plan tasks land; a checked item means its listed evidence was observed, not merely
implemented.

## T00 — Reproducible baseline and truthful guidance

**Status:** verified on 2026-09-08.

- Worktree verified: `/Users/aldrinjoseph/Developer/llm_agent_eval/.worktrees/workflow-implementation`.
- Existing default baseline before T00's new tests:
  `PYTHONPATH=src python -m pytest -q` → **513 passed, 3 skipped, 3 deselected, 2 warnings** in
  27.30 seconds. The default marker expression deselects `live`; skipped tests are not evidence
  for provider or external-service behavior. Warnings: Pydantic reports `Evaluator.schema`
  shadows `BaseModel.schema`, and pytest declines to collect the Pydantic `TestCase` model in
  `tests/test_judge.py`.
- Packaging: `python-multipart>=0.0.20` is declared because FastAPI's ZIP-upload route requires
  it. `requirements.lock` resolves runtime packages and `requirements-dev.lock` resolves runtime
  plus pytest development packages. They are generated with `uv pip compile` from `pyproject.toml`
  and contain package versions only—no provider or database credentials.
- Clean-install smoke observed in a fresh temporary `uv` environment: synchronizing
  `requirements-dev.lock`, installing this package with `--no-deps`, `eval-engine --help`, and
  constructing `create_app` with a temporary SQLite database all succeeded. The constructed app
  includes `/api/projects/upload`.
- Automated coverage: `tests/test_api.py` verifies the app factory builds both health and archive
  upload routes using a temporary database. `tests/test_installation.py` verifies the package
  declaration and an independently installed CLI/app-factory smoke. `integration` and `browser`
  markers are registered for their future suites; neither suite exists yet. `live` remains opt-in.
- Final default-suite evidence after T00 coverage: **516 passed, 3 skipped, 3 deselected, 2
  warnings**. The suite was observed in two passing invocations to remain below the local command
  timeout. A fresh lock-synchronized environment installed `psycopg==3.3.5` and then observed all
  three PostgreSQL checks skip on a real connection refusal to `127.0.0.1:5432`; they are not
  credited as integration verification.
- Container guidance: the image installs the runtime lock and contains no database URL. The
  pre-existing Compose configuration starts a prototype API plus local PostgreSQL; it has not
  verified the future sandbox/proxy or hosted-agent workflow and must not be represented as doing
  so.

## Current boundary

The package currently provides a FastAPI/CLI prototype, local development storage, evaluation
semantics, ZIP/Git ingestion, evidence-backed knowledge versions, CEL for new scalar
predicates, durable harness sessions with a LiteLLM SDK gateway (no LiteLLM Proxy server
in the run path), versioned dataset import with label provenance, deterministic
synthetic dashboard previews from a built-in skill, and hosted synchronous JSON
target verification behind an exact test-only endpoint allowlist. Rootless
sandbox/proxy proof and shipping PostgreSQL RLS deployment remain later
workflow-plan work. T10 has a browser-verified onboarding slice, but its
durable API onboarding and end-to-end acceptance criteria remain incomplete.
T12 has a rootless Podman setup preflight, not a verified containment boundary.
The locked v3 architecture continues to govern those tasks; this record does
not relax its invariants.

## T01 — Truthful source and verification state

**Status:** verified on 2026-09-08.

- Source analysis now reports its observed source ID, static findings, and an explicit `not_verified`
  runtime state; it never substitutes the sample agent for an unknown path. Schema validation is
  separately reported as validation, not smoke success.
- Focused API and workflow compatibility checks passed. The frontend specialist also corrected
  status/recovery rendering and static syntax checks passed. The final 390×844 long-path browser
  check observed the source-not-found recovery card at equal client/scroll widths (232px), with no
  horizontal overflow.

## T02 — Durable versions, authorization, and artifact storage

**Status:** verified on 2026-09-08.

- Immutable, workspace-scoped version records use a compare-and-swap active pointer; valid legacy
  records migrate to snapshots while invalid definitions are quarantined without changing source
  rows. Artifact bytes are checksum-verified, staged, atomically published, and served only inside
  the authenticated workspace.
- PostgreSQL is exercised on a disposable server with the unprivileged application role. It proves
  RLS reads/inserts, `WITH CHECK` rejection, transaction-local tenant context after pooled
  connection reuse, and forced RLS on the full table inventory. The service startup path now
  distinguishes maintenance bootstrap from the non-bypass app login; Compose runs the bootstrap
  separately and starts the engine as that app login.
- Evidence: `tests/workflow` plus affected API tests passed (**61 passed**); the real PostgreSQL
  storage suite passed (**3 passed**) and RLS integration suite passed (**3 passed**) in fresh
  temporary locked environments. `docker compose config --quiet`, compilation, and whitespace
  checks passed. The complete default suite is re-run at the next task boundary.

## T03 — Durable jobs, secret references, and trusted redaction

**Status:** verified on 2026-09-08.

- The durable queue has idempotent enqueue, persisted outbox events, lease expiry/reclaim, fencing,
  cancellation intent, and terminal-state enforcement. Command fingerprints use an install-owned
  keyed HMAC rather than a raw secret-bearing digest.
- Secret ciphertext uses install-owned key material outside the repository. Resolution derives its
  authorized service solely from the running process identity, never a request value; service
  access and rotation are audited.
- Storage redacts trace payloads and errors before persistence and sets `redacted` or `truncated`
  evidence state from the observed redaction result, including truncation with no detector match.
- Evidence: affected SQLite/API/workflow suite **104 passed**; real PostgreSQL job-recovery and
  RLS suite **4 passed** in a fresh temporary environment. Independent re-review found no P0–P2
  issues after remediation.

## T04 — Secure immutable Git and ZIP ingestion

**Status:** verified on 2026-09-08.

- Streaming uploads enforce archive limits before extraction; raw uploads are stored in an isolated,
  permission-restricted quarantine directory (0o700/0o600) and never appear in public manifests or
  download routes.
- Pre-extraction inspections strictly reject path traversals, absolute/drive/UNC paths, symlinks and
  special files, case/Unicode name collisions, encrypted ZIPs, expansion bombs, and high compression
  ratios.
- Sanitized snapshots exclude `.env` files and hardcoded API tokens (`sk-test-do-not-persist-0001`) from
  stored readable artifacts while recording structured exclusion provenance.
- Syntax parse diagnostics are captured on non-executing AST parse failures (e.g. broken reference agent
  syntax), producing diagnostic records while blocking runtime readiness (`readiness: blocked`).
- Approved HTTPS Git acquisition enforces allowed hosts (`github.com`, `gitlab.com`, `bitbucket.org`),
  disables submodules, hooks, and LFS implicit execution, redacts credential transport, and rejects
  unapproved hosts with `git_source_rejected` and unresolvable refs with `git_ref_not_found`.
- Evidence: focused import contract and archive limits suite **16 passed**; full test suite **559 passed,
  7 skipped, 3 deselected, 3 warnings**.

## T05 — Evidence-backed discovery, confirmed knowledge, and retrieval

**Status:** verified on 2026-09-08.

- Nested Python sources are parsed as data. Runtime-dict tools are stored as `inferred` facts
  at confidence 0.3 with file/line provenance. No fact carries `observed_in_attempt_id` or a
  verification id until an actual run exists. Missing `kickoff`/`run`/`invoke` sets
  `needs_entrypoint_declaration` and a stable pending question.
- README/system-prompt text is not treated as static truth. Confirmation writes an immutable
  knowledge version; a later source snapshot that removes a confirmed fact marks it
  `review_needed` and keeps the original pending question across process restart.
- Bounded lexical retrieval returns cited `.py` windows from the sanitized snapshot only.
- Evidence: `tests/workflow/test_knowledge.py` plus version contracts **9 passed**.

## T06 — CEL and evaluator compatibility migration

**Status:** verified on 2026-09-08.

- Scalar predicates keep the prototype closed grammar as `language_version=prototype` so stored
  rules are not silently reinterpreted. CEL (`cel-python==0.5.0`) compiles map/index expressions
  and the v3 variable set. Unknown identifiers fail at compile time. `cel_predicate` metrics
  validate by compiling CEL. Trace-rule `where` clauses that use CEL indexing compile as CEL;
  adapter events remain first-class `for_all` evidence. `on_error: fail` counts ERROR in the
  denominator (16 pass / 2 fail / 1 error / 1 missing → 80%).
- Evidence: CEL contract plus predicate/trace/evaluator/spec suites **182 passed**. Default
  suite after T05+T06: **569 passed, 7 skipped, 3 deselected, 4 warnings**.

## T07 — Stateful harness, model gateway, and typed metric proposals

**Status:** verified on 2026-09-08.

- Sessions and redacted messages persist with evaluation version pointers. Turns enqueue
  durable jobs; the worker loads the current evaluation and applies a typed metric patch.
  Production authoring does not keyword-replace spec text. Bounded repair stops after two
  failed repairs and leaves the prior draft intact (`rejected`). A keyless LiteLLM SDK
  gateway records `failed` without disabling GET of the session or evaluation. The gateway
  never sets `temperature` and never starts a LiteLLM Proxy server.
- Scenario `authored_session`: three metrics, no verification. “Make the latency limit two
  seconds” patches only `latency` to `raw_value <= 2000` and leaves the other metrics
  unchanged across restart.
- Evidence: session/import/gateway focused suite **34 passed**; workflow+gateway+harness
  **62 passed**. `litellm>=1.63.0` is pinned through `requirements.lock` /
  `requirements-dev.lock` (`litellm==1.100.0`).

## T08 — Dataset mapping, labels, provenance, and selection

**Status:** verified on 2026-09-08.

- CSV, JSON, and JSONL imports use deterministic JSON Pointer or column mapping. Null and
  missing fields stay distinct. Duplicate/contradictory identities are rejected with
  `duplicate_identity`. Case input over 256 KiB fails as `case_input_limit` and is not
  dropped. Golden expected values are stored separately from `agent_request`.
- Scenario `mixed_label_dataset`: four cases, two approved labels, one inferred, one missing.
  Health metrics are eligible on all four; accuracy is eligible on two (`2/4`). Explicit
  exclusions write a new immutable version; the prior version digest is unchanged.
- Evidence: `tests/workflow/test_datasets.py` plus session/import contracts **22 passed**.
  Default suite after T07+T08: **582 passed, 7 skipped, 3 deselected, 4 warnings**.

## T09 — Canonical dashboard skill, deterministic preview, and saved artifacts

**Status:** verified on 2026-09-08.

- Canonical registry identities (`MetricCard`, `RunSummary`, `TestCaseTable`,
  `TraceTimeline`, …) alias the prototype components. `HTMLPreview` is rejected.
  Legacy lowercase names migrate when snapshotting dashboard definitions.
- Built-in skill `skills/dashboard-authoring/SKILL.md` plus `schema.json` is the
  only skill path; an uploaded `SKILL.md` does not change generator output.
- Scenario `declared_range_dashboard`: latency has a numeric range sample;
  eligibility without a numeric range is a placeholder. Two preview jobs share
  `data_hash`. `data_kind` is `synthetic` and `gate_result` is null. HTML carries
  the permanent synthetic banner and no scripts. Artifacts are definition, data,
  HTML, and manifest with a presentation-only review receipt.
- Evidence: workflow suite **41 passed**; dashboard unit plus workflow **78 passed**.
  Default suite after T09: **587 passed, 7 skipped, 3 deselected, 4 warnings**.

## T11 — Hosted JSON target, network policy, and real verification

**Status:** verified on 2026-09-08.

- `HttpJsonAdapter` implements `verify`/`invoke`/`cancel`. Smoke is a real POST of
  `smoke_input`; a GET `/health` is not counted. Capabilities for hosted JSON are
  `final_output: observed` with `tool_execution` and `provider_cost` unavailable.
- Production `EndpointPolicy` rejects loopback and link-local metadata. Tests permit
  only `EVAL_ENGINE_ALLOW_ENDPOINTS` exact `host:port` entries. DNS rebinding to
  loopback is `dns_rebinding`. Redirects are not followed (`redirect_blocked`).
- Inline bearer tokens are rejected (`secret_ref_required`). Golden keys cannot appear
  in request/output mappings. Streaming/stateful modes return `unsupported_target_mode`.
- OpenAPI import allows bundled `#/` refs only, rejects external `$ref`, and requires
  an explicit operation before mapping assistance is returned.
- Typed outcomes: `auth_denied`, `timeout`, `mapping_error`, `oversized_response`,
  `error_envelope`, `redirect_blocked`. Scenario `output_only_api` uses
  `tests/fixtures/http_agent_server.py`.
- Evidence: `tests/workflow/test_http_target.py`, `test_http_outcomes.py`, and
  `tests/integration/test_endpoint_policy.py` **11 passed** (RLS suite skipped without
  `TEST_DATABASE_URL`). Default suite after T11: **598 passed, 7 skipped, 3
  deselected, 4 warnings**.

## T10 — Nontechnical authoring interface

**Status:** partial, browser slice verified on 2026-09-09; do not treat as
complete.

- The visible legacy builder is replaced by a review-first authoring workspace
  with reference, metrics, dataset, and preview tabs. Metric selection supports
  multi-select, custom intent, explicit missing/error policies, definition
  inspection, and browser-local draft recovery. It never starts an evaluation;
  an optional supplied session ID can resume a persisted session and queue a
  typed proposal.
- A reviewed onboarding extension accepts an existing project ID, queues existing
  Git/ZIP import routes, displays server-backed knowledge reports and pending
  entrypoint questions, confirms findings, and restores sessions. It explicitly
  names unsupported project creation/listing, free-text correction, and job
  completion polling rather than simulating them. Project changes invalidate
  report/session state; ZIP bytes remain binary; confirmation restores keyboard
  focus to the report heading.
- Browser evidence: `npm run test:browser --
  tests/browser/authoring/authoring.spec.js` → **11 passed**. An independent
  review reproduced and then approved fixes for ZIP transport, stale report and
  session state, pending questions, and keyboard focus. The harness loads the
  static page directly because this execution environment's browser cannot
  reach a listening localhost server; it therefore proves browser interaction
  and injected server responses, not live FastAPI/worker integration.
- Remaining: complete browser-backed project bootstrap, worker completion
  reconciliation, free-text corrections, dataset mapping/label review, canonical
  preview save/revision/undo, cross-tab conflict recovery, real API visual
  checks, and the required desktop/mobile/accessibility acceptance scenarios.

## T12a — Rootless runtime preflight

**Status:** partial, reviewed on 2026-09-10; do not treat as containment
verification.

- `AGENT_RUNTIME` accepts only the approved `runc`, `runsc`, and `vm` tiers.
  The preflight uses bounded Podman CLI argument vectors and emits typed JSON
  states. It rejects a missing binary, invalid tier, rootful runtime/root socket,
  credential-bearing socket URI, and `runsc` outside Linux; Docker and the
  trusted subprocess runner are never selected as fallbacks.
- On this macOS host, a newly installed Podman 6.1.1 Machine exposes its rootless
  user connection and preflight reports `ready_for_containment_experiment`.
  Explicit sockets are checked independently of an unrelated default connection.
- Evidence: `tests/test_runtime_preflight.py` **10 passed**; the live preflight
  reports engine/API `6.1.1`. Independent review and two scoped fix rounds found
  no remaining P0–P2 issue.
- Remaining: per-case agent network/proxy topology, egress-bypass proofs,
  per-install CA generation/trust injection, packet/connection evidence,
  read-only roots and bounded scratch, production-run cleanup verification,
  and fresh-case execution. These are required before any uploaded agent can
  run.

## T12b — Internal-network containment probe

**Status:** partial, observed on 2026-09-10; this is a narrow topology
observation, not a containment certification.

- On the observed macOS host, rootless Podman engine/API `6.1.1` passed the
  preflight and containment-focused suite: **25 passed**. The explicitly
  enabled live check, `LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1 ... -m live
  tests/integration/test_egress_boundary.py`, then reported **1 passed**. The
  enabled evidence runs reported no skips or failures.
- That live pass observed one rootless internal Podman network where the client
  reached its HTTP witness, the fixed direct dial was blocked, and the exact
  temporary containers and network were removed. It is evidence for that
  host/engine at that time only.
- It does not prove proxy-only egress; CA generation or TLS interception;
  DNS, IPv6, UDP, or redirect coverage; credential brokering; agent
  image/source execution; fresh cases; packet-level enforcement; or broad
  platform support. Those remain required before dispatching uploaded agents.
- `scripts/spin_up.sh` now performs only the structured rootless setup
  preflight. A ready result does not start an agent runtime or prove
  containment; prototype API development remains separately documented in
  `README.md`.

## Release blockers 1–5 (2026-09-17/18 branch work)

**Status:** partial, implemented and unit-verified on `feat/release-blockers`;
this is component evidence, not a completed release path. Nothing here is
merged or released, and the end-to-end acceptance item (blocker 10) remains
open.

- **Sandbox (blocker 1).** `src/llm_agent_eval/runtime/sandbox.py` builds a
  per-case rootless Podman command vector: digest-pinned image, `--pull=never`,
  `--network=none`, read-only root, dropped capabilities, no-new-privileges,
  empty environment, bounded tmpfs output, read-only bind mounts of
  engine-owned staging copies, digest re-verification, and exists-then-remove
  cleanup. Evidence: `tests/runtime/test_sandbox.py` (policy/argument-vector and
  fail-closed tests) passes. A manual synthetic smoke on Podman 6.1.1 ran
  two cases from the locally installed Alpine image ID: both returned
  `completed`, produced the expected `result.json`, used distinct container
  names, and reported cleanup `complete`. This proves that narrow offline
  path on this host, not adversarial containment or platform portability.
  **Not yet proven:** any proxy-only egress topology — `SandboxRequest`
  still rejects every `egress` value except `none`.
- **Recording egress proxy (blocker 2).** `src/llm_agent_eval/egress/` provides
  the per-install interception CA (0600 persistence, repository-parent guard,
  self-signature verification, short-lived per-host leaves) and a
  metadata-only recording forwarder with dummy→real credential substitution,
  observed-usage metering, and worst-case budget reservation. Evidence:
  `tests/test_egress_foundations.py` with injected synthetic transport responses;
  no network listener or live provider call was exercised. **Not yet built:** the
  network listener/topology that makes the proxy the sandbox's sole route, CA
  trust injection into a real container, and live TLS interception. Until that
  exists, the sole-egress invariant is unverified.
- **Durable worker (blocker 3).** Continuous claims, heartbeat leases,
  cancellation, stale-worker fencing, and fenced publication are implemented in
  `worker_service.py`/`jobs.py` with a `worker` CLI subcommand and
  `eval-engine worker` Compose service. Evidence:
  `tests/workflow/test_worker_service.py` plus `tests/test_worker_cli.py` pass.
  The PostgreSQL recovery integration suite
  (`tests/integration/test_worker_recovery.py`) requires an explicit disposable
  `TEST_DATABASE_URL` and **skipped in all observed runs** — it is not credited
  as execution evidence. The recovery test simulates lease expiry with an
  injected clock on one process; it does not kill and restart a real worker
  process.
- **Outbound HTTP/SSRF hardening (blocker 4).** DNS-pinned connect with the
  original TLS hostname, streamed bounded bodies, no ambient proxies, HTTPS for
  auth-bearing requests. Evidence: `tests/workflow/test_pinned_http.py`.
- **AEAD secret encryption (blocker 5).** Versioned AES-GCM envelopes bound to
  workspace/secret identity via AAD, legacy decode-only migration gated on
  process service identity, persisted random install/fingerprint keys with
  `0600` enforcement. Evidence: `tests/workflow/test_secrets.py`.
  `cryptography` is declared in `pyproject.toml` and pinned in both regenerated
  lockfiles; the clean-install smoke passes.
- **Model profiles and judges (blockers 6/7).** `profiles.py`,
  `rubric_store.py`, `judge_gateway.py` add provider profiles, immutable
  rubrics, and a gateway judge that validates evidence membership; the engine's
  default judge is now fail-closed (`UnconfiguredJudge`) instead of the
  deterministic fixture, so unconfigured judge metrics error rather than
  fabricate scores. Evidence: `tests/test_profiles.py`, `tests/test_rubric_store.py`,
  `tests/test_judge_gateway.py`, `tests/test_engine_judge_default.py`.
  **Not yet wired:** no run path constructs `GatewayJudge` with real rubric/
  context resolvers, or selects persisted profiles in release services.
  Generic API writes for profile, selection and rubric kinds now return
  `protected_version_kind` without publication; dedicated validated creation
  endpoints remain to be added.
- **Legacy path lockdown (blocker 8).** In release mode the API filters legacy
  mutation routes (`/api/projects/analyze`, `/runs*` mutations, harness chat/
  author, sample runs, eval runs, import drain). Evidence:
  `tests/test_release_routes.py`.
- **Deployment (blocker 9).** Compose gains a continuous `eval-worker` service
  sharing the persistent artifact volume; CI gains fail-closed test, dependency
  audit, and secret-scan jobs with a disposable PostgreSQL service. Evidence:
  `tests/test_release_deployment.py` and `docker compose config` structure
  checks with placeholder secrets. The CI workflow is not executed on GitHub,
  and `compose up` was not run — neither is claimed.
- **Suite state at recording time:** default suite **801 passed, 9 skipped,
  4 deselected**; the 9 skips are PostgreSQL-dependent tests (no
  `TEST_DATABASE_URL`/local server) and are not release evidence.

**Remaining release gaps (all ten blockers re-checked 2026-09-18):** no
`evaluation_run` worker handler or container-target admission (source versions
stay `readiness="blocked"`; run creation still freezes host paths); no
proxy listener, trust injection, or provider transport, so proxy-only egress is
unproven; `GatewayJudge` has no run-path resolver wiring; the engine still runs
agents as host subprocesses outside the workflow worker; end-to-end acceptance
(upload → isolated execution → authoritative capture → scoring → restart
recovery) has not been exercised even with synthetic fixtures.

## R1/R2 follow-up on `codex/r1-r2-complete`

**Status:** partial, verified on 2026-09-19; this section supersedes the
historical “remaining gaps” wording above only for the evidence listed here.
It is not an R1 release declaration.

- **Frozen worker execution:** `evaluation_run` is now dispatched by the
  durable worker. Run plans require immutable evaluation/dataset references,
  verified non-stale targets, and explicit authorization. Local executable
  sources are materialized inside worker-owned staging; target invocation no
  longer persists a host source path in the run manifest. Hosted output-only
  runs and a judge-backed run are covered by integration tests.
- **Output evidence and re-score:** target/local output is redacted before
  persistence in `attempt_outputs`; deterministic raw values are stored in
  case results. `POST /runs/{run_id}/metrics/{metric_id}/rescore` creates a new
  score revision from retained output/traces without invoking the target. The
  hosted end-to-end test verifies the measured endpoint call count is unchanged
  by re-score.
- **Comparison:** paired comparisons now check metric identity, dataset and
  expected-reference identity, world identity, matched/scored cohort size and
  authoritative scores. They return explicit incomparable states, disclose
  source/target/model/repeat confounders, and omit the confidence interval when
  repeats are below the minimum.
- **Proxy/network seam:** a bounded listener, per-run CA/session, trusted
  dual-homed Podman relay, managed internal network, and sandbox proxy
  argument vector exist. The live opt-in suite now passes the relay/provider
  HTTP proof, host-side TLS interception with the install CA, and a full
  restricted-build → fresh-container → proxy → output → scoring run. Proxy
  capture records are now bound into the worker's authoritative persisted
  trace, including typed usage/cost metadata and capture timestamps. The
  worker acceptance test covers upload → restricted build → authorization →
  per-case proxy run → output → scoring. The
  container proof uses a raw HTTP relay client because the pinned Alpine
  BusyBox client does not implement the required HTTPS CONNECT flow; a real
  provider-client HTTPS proof from inside the case remains open.
- **Production proxy admission:** the default workflow worker now constructs
  a trusted `ProxyRunSession` factory from an installation-owned
  `proxy-routes.json`; route files reject unsafe permissions, duplicates,
  missing metadata, untrusted token counters, and embedded secret fields.
  The worker resolves encrypted secret references only at the outbound proxy
  boundary. Actual provider route configuration and the full containerized
  runtime topology remain deployment evidence requirements.
- **Durable judge calibration:** calibration rows and label lineage now persist
  by workspace and exact binding `(provider, model, schema, rubric)`, with
  generation-based reset and `UNCALIBRATED → CALIBRATING → CALIBRATED`
  thresholds. Engine and re-score paths read the persisted readiness registry;
  API routes support label, status, and reset operations. Human dispute/review
  UX and a complete calibrated run acceptance scenario remain open.
- **Provider protocol coverage:** the recording proxy now validates and meters
  tested OpenAI, Anthropic, and Google request/usage shapes with provider-
  specific dummy-key substitution. Supported HTTPS routes now use the
  embedded LiteLLM Router outbound seam, while generic/local HTTP remains an
  explicit fixture fallback. Full stream-aware provider interception, live
  provider-route proof, and repaired CrewAI/provider fixtures remain open.
- **Operations:** local readiness, workspace artifact usage/quota primitives,
  orphan recovery, and non-overwriting SQLite workspace backup/restore with
  manifest checksums are implemented. Coordinated PostgreSQL + artifact-store
  backup/restore, retention enforcement, export/preview TTLs, and a real
  cross-process recovery rehearsal remain open. The authenticated run now has
  CSV and HTML export routes; `POST /api/exports` persists workspace-scoped
  frozen manifests with JSON/CSV/HTML hashes, and `ci-status` exposes the
  documented 0/1/2 contract. Frozen exports now retain per-case metric scores,
  raw values, evidence event IDs, authority/retry markers, and richer CSV/HTML
  evidence rows. Export/preview TTLs and the full bundle/CI acceptance scenario
  remain open. Schedule USD limits now
  reconcile completed slots from authoritative run cost ledgers and reserve
  worst-case spend for in-flight slots. Scheduled slots use distinct per-slot
  run idempotency keys instead of collapsing into one plan-level run.
- **Compose/runtime smoke:** a disposable `docker compose up -d --build`
  completed with PostgreSQL healthy, the API and worker running, `/health`
  returning 200, and `/readiness` returning 200 with database and mounted
  artifact-root checks passing. This validates prototype service startup only;
  it does not prove the rootless Podman sandbox topology.
- **R2 adapters:** streaming, async-job, and scripted stateful HTTP adapters
  are opt-in behind `EVAL_ENGINE_ENABLE_R2=true`; streaming and async targets
  run through the frozen worker path in `tests/integration/test_r2_run_e2e.py`.
  Streaming emits adapter-authored `stream_start`, `first_token`, and
  `llm_response` evidence; stateful scripts enforce wait-for-input before a
  user response and close sessions after rejection. Remote-job identity is
  persisted before polling; stale-attempt reconciliation now marks the job
  orphaned and a replacement attempt resumes polling that same remote job
  without a second submit. Async/stream cancellation lands as a cancelled
  attempt with explicit remote uncertainty, and proxy evidence is retained
  even when the adapter ends in cancellation, timeout, or provider failure.
  Stateful session targets now have a full worker-path acceptance case covering
  verification, scripted approval, multi-turn invocation, cleanup, and scoring.
  Retrieval now also has an explicit `retrieval` target over authoritative
  ranked-chunk events; missing retrieval follows `on_missing`. Stateless JSON
  targets now accept a closed retrieval mapping and emit capture-time-redacted
  adapter retrieval events, with a conformance matrix that lists only the tested
  generic HTTP adapter and returns `unsupported_framework` for unverified
  frameworks. Schedules enforce the implemented frozen-version policy rather
  than accepting an unimplemented refresh policy. Retrieval adapter/framework
  conformance beyond generic HTTP remains open.
- **Observed suite:** the default suite is **897 passed, 9 skipped, 7
  deselected, 4 warnings**. Skips remain PostgreSQL/live-environment checks;
  they are not credited as release evidence. The opt-in live proxy/sandbox
  suite passes **4 tests** on the observed macOS Podman 6.1.1 host.

**Still release-blocking:** the specialist-owned authoring/results UI is not
complete; the trusted proxy-route/session factory exists, but its configured
route file, credential setup, and full containerized deployment topology are
not yet release-proven; worker restart recovery is covered at the
adapter/engine seam but not by a killed-process live acceptance run;
provider-client HTTPS interception from inside the case and
direct/alternate IPv4/IPv6/DNS/UDP/redirect bypass tests are not proven on
Linux, macOS and WSL2; real PostgreSQL recovery, backup/restore, retention,
TTL, and fairness are not rehearsed; LiteLLM
outbound/cassette fidelity, repaired CrewAI execution, full custom evaluator
sandbox integration, full export-bundle/CI acceptance, and the complete
E01–E32 / UX acceptance matrix remain unfinished.
