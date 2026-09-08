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
semantics, and prototype ZIP intake. The secure bounded source-ingestion service, rootless
per-case runtime, recording egress proxy, credential broker, durable worker/outbox, shipping
PostgreSQL RLS deployment, and hosted-agent adapter remain later workflow-plan work. The locked
v3 architecture continues to govern those tasks; this record does not relax its invariants.

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
