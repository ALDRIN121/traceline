# v2 Design Review — Why v3 Changes the Mechanisms

## Status and reading order

This is the standing rationale record for the LLM Agent Evaluation Engine. It
records the design failures found in v2 and the decisions that close them.
Read [LLM_Agent_Evaluation_Engine_v3.md](LLM_Agent_Evaluation_Engine_v3.md)
for the normative specification. Where this review and v3 disagree, v3 wins.

The product thesis in v2 remains sound: an evaluation engine, not an
LLM-as-a-judge wrapper; deterministic evaluation first; evidence for every
score; an LLM only for authoring and explanation. v3 retains that thesis but
specifies the previously missing execution, tracing, evaluation, and safety
contracts.

## Findings

### Load-bearing omissions

| ID | v2 gap | Consequence | v3 resolution |
|---|---|---|---|
| G1 | Trace event names but no capture mechanism or payload contracts. | No trace, tool, or evidence metric can be implemented. | §12A–§12C: recording egress proxy, enrichment shim, canonical event contract. |
| G2 | No invocation, image-build, dependency, or output protocol. | A runner is impossible to implement consistently. | §11A: mounted input/output protocol and pinned image build. |
| G3 | No fixture world despite expected tool outputs and closed network. | Useful agents are either flaky or cannot run. | §10A: fixtures, cassettes, and proxy fault injection. |
| G4 | No repeats, comparability, or statistical regression model. | Noise looks like a regression and regressions hide in noise. | §11C and §21A: stable case keys, repeats, confidence intervals, matched comparison. |
| G5 | Trace rules and scalar rules are prose / arbitrary code. | Implementers must invent a parser or unsafe evaluator. | §9A: closed JSON trace operators plus CEL scalar predicates. |
| G6 | Missing-target and evaluator-error semantics are unspecified. | Safety rules can pass vacuously. | §7A: required `on_missing` and `on_error`; no defaults. |
| G7 | Case scoring, aggregation, and gate behavior are conflated. | The same specification yields different headline results. | §7A and §13A: separate per-case, run aggregation, and gate fields. |
| G8 | No executable-agent gate. | Users author metrics for agents that cannot start. | §32A and §4B: DECLARE → PREPARE → SMOKE recovery flow. |
| G9 | Run-scoped results have no model or storage. | p95, cost, failure budgets, and gates cannot persist. | §13A and §18A: separate run-metric results and score revisions. |
| G10 | Source hashes do not capture resolved runtime configuration. | A run cannot be reproduced. | §20A: immutable manifest including image digest and resolved model binding. |
| G11 | Preview uses undefined mock generation and two dashboard registries. | Approval is based on invented, untrustworthy data. | §14A and §37B: labelled illustrative previews and one validated registry. |
| G12 | Judge rubric, result schema, calibration, caching, and variance are absent. | Semantic scoring is not a defined instrument. | §15A: validated judge contract and readiness states. |

### Contradictions corrected

| Conflict | Correction in v3 |
|---|---|
| Containers must never see platform credentials, but agents need providers. | The sandbox has a dummy key; the recording proxy substitutes credentials and enforces the budget. |
| LiteLLM Proxy was considered an egress proxy. | It is an API server, not a forwarding CONNECT proxy. A custom proxy embeds the LiteLLM SDK/Router only for outbound routing. |
| Local tools are invisible to network capture. | A framework-aware shim enriches traces over `/_enrich`; its events are evidence but never authority over proxy facts. |
| Production replay is a core use case but enabling monitoring is future work. | Production replay is deferred; cassette replay remains as its mechanism-level prerequisite. |
| A mandatory large run conflicts with confirmation and worker limits. | Argument-sensitive confirmation and Quick/Standard/Full run tiers govern all runs. |
| Static analysis presents discoveries as facts. | Every discovery carries confidence and provenance; smoke evidence can confirm it. |

### Missing use cases elevated into the specification

v3 gives each category an evaluator strategy rather than leaving it
aspirational: multi-turn conversations and approval gates use interaction
scripts; prompt injection, PII discipline, termination, tool parsimony, and
resilience use deterministic trace rules; RAG uses retrieval events plus
reference / judge checks; multi-agent work uses delegation and adapter events;
streaming uses first-token events; cost uses the proxy ledger; stability uses
repeat distributions; and human labels calibrate judges. Pairwise A/B,
production replay UI, and full CI configuration are deliberately deferred,
while their underlying contracts remain.

### Operational failures prevented by v3

- Redact before persistence, embedding, audit explanation, or dashboarding.
- Use one fresh container and internal network per case; do not reuse agent
  state between cases.
- Cap archives, build time, process output, event counts, event payloads, and
  run size; retain raw trace data by dropping run partitions.
- Keep retries separate from repeats. The first attempt is authoritative;
  retries are diagnostic and may never mask flakiness.
- Keep untrusted custom evaluators out of the control plane, and never mount a
  Docker socket into a sandbox.
- Partition evidence storage and use incremental aggregates so dashboards
  never scan raw traces for their common paths.

## Reality check: reference agent

`fixtures/reference-agent/multi_agent_system.py` is intentionally not
repaired. It is the acceptance walkthrough for v3. The system must:

1. reject its truncated source at parse gate before dependency installation;
2. present its missing `Crew(...).kickoff()` entrypoint as a declaration and
   recovery state rather than as an opaque failure;
3. run with no developer-installed tracer, capturing Gemini traffic at the
   proxy and local tool activity through the shim;
4. mark runtime-dict tool discovery as low confidence until smoke evidence
   confirms it;
5. expose unseeded mock randomness as flakiness under repeat execution; and
6. classify its LiteLLM-formatted Gemini model string as runtime
   misconfiguration, not sandbox failure.

The hosted example is a distinct, repaired, versioned fixture. Its repair is
disclosed; it never replaces the adversarial source fixture.

## Locked positions carried into v3

- Apache-2.0, open source, self-hosted, no telemetry, no bundled credentials.
- Multi-tenant schema with `workspace_id` and PostgreSQL RLS; single-tenant
  Compose deployment.
- Rootless Podman is the default, with isolation tiers selected only by
  `AGENT_RUNTIME`; macOS uses Podman Machine and Windows uses WSL2.
- A per-install TLS interception CA is generated locally and is never
  committed. This is a non-negotiable security boundary.
- The harness is framework-free, using raw SDKs behind `ModelGateway`.
- The LLM emits validated JSON and never executable code or a string DSL.
- LLM judges are usable while `UNCALIBRATED`, but their scores are marked
  provisional and excluded from enforcement until calibration.

## Verification checklist for v3

The v3 review must demonstrate a named resolution for G1–G12, every
contradiction above, and every elevated use case. It must walk the reference
agent through ingestion, smoke recovery, proxy capture, adapter evidence,
metric authoring, scoring, evidence resolution, and a confirmation-gated run.
No `TBD` or unspecified default is acceptable in the contracts that control
safety, scoring, or reproducibility.
