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
sandbox/proxy and shipping PostgreSQL RLS deployment remain later workflow-plan
work. T10 has a browser-verified static authoring slice, but its durable API
onboarding and end-to-end acceptance criteria remain incomplete. The locked v3
architecture continues to govern those tasks; this record does not relax its
invariants.

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
- Browser evidence: `npm run test:browser --
  tests/browser/authoring/authoring.spec.js` → **2 passed**. The harness loads
  the static page directly because this execution environment's browser cannot
  reach a listening localhost server; it therefore proves the static interaction
  slice only, not FastAPI integration.
- Remaining: browser-backed Git/ZIP-to-project onboarding, report correction,
  persisted metric-result display, dataset mapping/label review, canonical
  preview save/revision/undo, cross-tab conflict recovery, real API visual
  checks, and the required desktop/mobile/accessibility acceptance scenarios.
