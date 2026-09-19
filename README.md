# Traceline

A configurable evaluation engine for agentic systems — a self-hosted source of truth for
execution, validation, scoring, and reproducibility.

Connect an agent codebase; Traceline parses it, helps you translate natural-language evaluation
intent into a structured specification, runs the agent against test cases, captures full
execution traces, scores them with configurable evaluators, and explains every result with links
back to the evidence that produced it.

**The core thesis: this is not an LLM-as-a-judge product.** The LLM authors and explains; a
deterministic engine executes and scores. Semantic judging is one evaluator type among many —
exact match, schema, rule, trace, numeric, reference, and custom code come first, because most of
what teams actually need to check can be stated exactly.

## Current implementation status

This repository is actively implementing the v3 design. The installed package now contains
verified slices of the durable worker, rootless sandbox/proxy, secure ingestion, hosted connector,
and R2 adapter paths, but it is not yet the complete v3 platform. Cross-platform containment,
production provider-route rehearsal, the full UI, broader PostgreSQL retention/link deployment audits, live execution
of the repaired reference fixture, and the complete release acceptance matrix remain open.
Coordinated backup /
restore code, owner-managed encrypted secret references, retention cleanup, and operational CLI
commands are available; the implementation status is the source of truth for what has actually
been verified.

See the [workflow implementation plan](docs/design/workflow-implementation-plan.md) and the
[implementation status](docs/design/workflow-implementation-status.md) for the verified boundary
between prototype behavior and planned work.

## What's available today

- **EvaluationSpec** — one validated JSON document defines cases, metrics, scoring, aggregation,
  and the run-level gate. An LLM harness (opt-in) translates intent into a spec; the engine never
  executes LLM output.
- **Deterministic-first evaluation foundation** — validated specs, exact match, schema, numeric,
  trace-rule, reference, and provisional semantic-judge building blocks.
- **Durable runs and evidence APIs** — local development storage, frozen worker run plans,
  redacted output/proxy evidence, declarative dashboard data, and CSV/JSON/HTML export routes.
  Full deployment-grade isolation and provider interception remain release-gated.
- **Evidence-linked dashboards** — a declarative definition renders run overviews, per-case
  tables, and trace evidence: every score is clickable down to the event that produced it.
- **Open-source local prototype** — Apache-2.0, no telemetry, no phone-home, and no bundled
  provider credentials. Tri-platform isolated execution is a design target, not a current claim.

## Quickstart

```bash
uv venv .venv
uv pip sync --python .venv/bin/python requirements-dev.lock
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/eval-engine serve       # prototype UI/API at http://127.0.0.1:8000
```

The current CLI can create a local project, run its prototype smoke gate, and execute a local
evaluation:

```bash
.venv/bin/eval-engine init my-agent-eval                       # spec template + project skeleton
.venv/bin/eval-engine smoke my-agent-eval                      # smoke gate: does the agent actually run?
.venv/bin/eval-engine run my-agent-eval/spec.json --entrypoint .venv/bin/python my-agent-eval/agent.py
.venv/bin/eval-engine results <run-id>                         # scores, gate status, revisions
.venv/bin/eval-engine ci-status <run-id>                       # JSON CI contract; 0/1/2 gate result
```

The dashboard is served by the same server as `eval-engine serve`. It is a prototype presentation
surface, not evidence of a completed hosted or sandboxed evaluation workflow.

To start the current containerized prototype and its local PostgreSQL service:

```bash
docker compose up --build
```

The Compose file's local database values are development-only defaults. Supply deployment
credentials through your environment or deployment secret manager; the image and lockfiles do not
contain database or provider credentials.

Proxy-enabled local runs also require an install-owned `proxy-routes.json` under the configured
artifact root. Start from [`proxy-routes.example.json`](proxy-routes.example.json), replace the
workspace secret reference and release price-table values, and keep the file non-writable by other
users. The file contains routing metadata only; provider keys stay in encrypted secret references
and are resolved by the worker's trusted proxy identity. The Compose worker sets that identity to
`proxy`; missing or unsafe route configuration fails a proxy run closed.

The repository also includes a separate, syntactically repaired reference copy at
[`fixtures/reference-agent-repaired/`](fixtures/reference-agent-repaired/). The original fixture
above remains deliberately broken and is never modified. Rootless Podman acceptance checks for
the proxy, an in-case HTTPS client, and a custom evaluator are opt-in and require the documented
local image/runtime setup; they are not counted in the default suite.

Authenticated API clients can freeze an export with `POST /api/exports` using a run ID and then
download its immutable `json`, `csv`, `html`, or evidence `bundle` representation from
`/api/exports/{export_id}/{format}`. The bundle is a deterministic ZIP containing the manifest,
CSV, HTML report, retained redacted evidence, missing-evidence references, provenance, and file
hashes. Later score revisions do not change a frozen export.

For PostgreSQL backup, provide a separate maintenance connection for `pg_dump`; the application
connection is RLS-scoped and is never used as an implicit backup authority. For example:

```bash
eval-engine backup workspace-backup.zip --workspace <workspace-id> \
  --postgres-url "$LLM_AGENT_EVAL_MAINTENANCE_DATABASE_URL"
eval-engine restore workspace-backup.zip --postgres-url "$DESTINATION_MAINTENANCE_DATABASE_URL" \
  --destination-artifact-root ./restored-artifacts
```

The deployment image carries PostgreSQL 16-compatible dump/restore binaries, and the archive
excludes install encryption and identity keys; those must be recovered through the operator's
separate key-management procedure. PostgreSQL bootstrap migrations are recorded in a
checksum-verified, forced-RLS journal and can safely replay a migration whose journal write was
interrupted; changed migration SQL is rejected.

R2 stateless JSON targets may also declare a closed `retrieval_mapping` with JSON Pointer fields
`chunks` (required), `query`, `scores`, and `source`. A successful response emits a redacted,
adapter-authored `retrieval` event; malformed mappings or unaligned scores fail closed. Retrieval
metrics therefore remain unavailable when a target has not declared authoritative ranked evidence.

## The reference fixture

[`fixtures/reference-agent/`](fixtures/reference-agent/) holds a real, deliberately unrepaired
CrewAI + Gemini agent. The smoke gate must FAIL it (and does) — the fixture exists so every design
proposal and every engine change is walked against a genuinely broken agent before being accepted.
[`fixtures/sample-agent/`](fixtures/sample-agent/) is the well-formed counterpart: a small
support-triage agent that runs offline or against any OpenAI-compatible endpoint.

## Design

The full design lives in [`docs/design/`](docs/design/). Start with the
[review-and-v3 plan](docs/design/v2-review-and-v3-plan.md), then the
[v3 specification](docs/design/LLM_Agent_Evaluation_Engine_v3.md). The prototype is being
progressively aligned with v3; section references in code (e.g. `§11A`) identify the intended
contract rather than claiming every v3 mechanism is complete.

## Tests

```bash
PYTHONPATH=src python -m pytest -q
```

The default suite excludes the opt-in `live` marker. Podman-backed acceptance checks are run
explicitly when the local runtime and pinned images are available:

```bash
LLM_AGENT_EVAL_RUN_PROXY_SANDBOX=1 PYTHONPATH=src python -m pytest -q tests/integration/test_proxy_sandbox_e2e.py -o addopts=''
LLM_AGENT_EVAL_RUN_PROXY_HTTPS=1 PYTHONPATH=src python -m pytest -q tests/integration/test_proxy_https_case.py -o addopts=''
LLM_AGENT_EVAL_RUN_CUSTOM_EVALUATOR=1 PYTHONPATH=src python -m pytest -q tests/integration/test_custom_evaluator_live.py -o addopts=''
```

Provider-backed live tests, when present, require explicitly supplied credentials in `.env`
(never committed) and should run last. `integration` and `browser` markers are registered for
the disposable-service and Playwright suites; passing those suites still does not prove the
cross-platform deployment matrix.

## License

Apache-2.0. See [LICENSE](LICENSE).
