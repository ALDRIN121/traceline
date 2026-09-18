# R1 and R2 End-to-End Completion Design

## Goal

Bring the repository from the current prototype boundary to the complete R1 workflow defined by the v3 design, then implement the four explicitly defined R2 extensions: streaming/async targets, stateful interactive evaluation, RAG/additional adapters, and recurring synthetic evaluation.

## Scope and precedence

The governing sources are, in order, `docs/design/v2-review-and-v3-plan.md`, `docs/design/LLM_Agent_Evaluation_Engine_v3.md`, `docs/design/end-to-end-workflow-plan.md`, and `docs/design/workflow-implementation-plan.md`. Existing implementation status is evidence about the current repository, not a relaxation of those requirements. The unrepaired `fixtures/reference-agent/` remains byte-for-byte unchanged; the repaired reference example is a separate versioned fixture.

## Architecture

The system keeps a modular control-plane monolith plus durable workers. API mutations create immutable, workspace-scoped versions and durable jobs. A run plan freezes all source, evaluation, dataset, target, dashboard, world, model, judge, runtime, and policy identities. Authorization binds to the plan hash. Workers execute the plan through either the hosted target adapter or the local target adapter, then send normalized attempt results through one scoring and evidence pipeline.

Local execution uses rootless Podman only. Builds happen in a restricted builder; every case/repeat/retry gets a fresh digest-pinned sandbox. The sandbox has no direct egress or credentials. A per-run recording proxy is its only network path, owns credential substitution and spend accounting, performs capture-time redaction, and emits authoritative provider evidence. The worker owns the runtime socket; the agent and custom evaluator never receive it.

R2 adds adapters behind the same `TargetAdapter` contract. Streaming and asynchronous targets normalize first-byte/first-token/final and persisted-remote-job states. Stateful targets receive explicit session/turn/reset/cleanup contracts. RAG adapters produce ranked retrieval evidence. Schedules enqueue frozen run plans with timezone, overlap, quota, and stale-verification policy.

## Invariants

- No uploaded source executes as a host subprocess.
- Every persisted object is workspace-scoped and authorized; PostgreSQL RLS is exercised with the non-owner application role.
- The proxy is the sole local-agent egress path; real provider credentials never enter the sandbox.
- Redaction occurs before persistence, indexing, cassette storage, or explanations.
- Adapter events are evidence but cannot claim trusted provider, cost, or redaction authority.
- Agent output never contains expected values or hidden credentials.
- First attempts remain authoritative; retries never replace repeat variance.
- Preview data is synthetic and cannot become a measured score.
- Mutations are idempotent and return observed lifecycle states.
- Unsupported protocols fail closed with typed outcomes.

## Delivery slices

1. R1 run-plan, authorization, target invocation contracts, and worker job admission.
2. R1 restricted builds, rootless sandbox, fresh-case local adapter, and runtime recovery.
3. R1 proxy topology, CA trust, protocol decoding, credential brokerage, budgets, capture, and cassettes.
4. R1 shared hosted/local execution, judge/profile wiring, comparison, calibration, re-score, exports, and CI API.
5. R1 live UI workflow, operations, cross-platform startup, and E01–E32 release rehearsal.
6. R2 streaming/async target protocols.
7. R2 stateful interactive evaluation.
8. R2 RAG and additional tested adapters.
9. R2 recurring synthetic evaluation.

## Completion evidence

Completion requires the full offline suite, PostgreSQL integration suite, browser suites against the live API, rootless runtime/proxy tests on Linux/macOS/WSL2 reference environments, clean installation checks, and the end-to-end acceptance scenarios in `end-to-end-workflow-plan.md`. A component unit test, a static browser test, or a successful preflight is not sufficient evidence for a broader boundary.
