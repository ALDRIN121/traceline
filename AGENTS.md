# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Start here

The repository contains an installable Python prototype and its design documents. The v3
specification is present at `docs/design/LLM_Agent_Evaluation_Engine_v3.md`; implementation work
is sequenced by `docs/design/workflow-implementation-plan.md` and its status record is
`docs/design/workflow-implementation-status.md`. Treat v3 and the workflow plan as design intent,
not proof that every planned service already ships.

Use the checked-in, pip-compatible lockfiles for a reproducible local environment:

```bash
uv venv .venv
uv pip sync --python .venv/bin/python requirements-dev.lock
uv pip install --python .venv/bin/python --no-deps -e .
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/eval-engine --help
```

The current package provides the FastAPI/CLI prototype, validated evaluation semantics, local
SQLite development storage, and a ZIP intake route. It does **not** yet ship the planned secure
Git/ZIP ingestion pipeline, rootless per-case sandbox/proxy, durable worker queue, full
PostgreSQL RLS deployment, or hosted-agent connector. `docker compose up --build` currently
starts the prototype API and its local PostgreSQL service; it is not evidence that the complete
isolation or hosted workflow is available.

```
docs/design/v2-review-and-v3-plan.md          # the plan of record — read this first
docs/design/LLM_Agent_Evaluation_Engine_v2.md # original spec; superseded on mechanisms
fixtures/reference-agent/                     # the design's reality check (deliberately broken)
```

## What this project is

A configurable evaluation engine for agentic systems: connect an agent codebase, the platform
parses it, an LLM harness turns natural-language intent into a structured `EvaluationSpec`, the
agent runs in an isolated container against test cases, traces are captured, configurable
evaluators score them, and dashboards explain results with links to evidence.

**Governing thesis: this is not an LLM-as-a-judge product.** The LLM is the authoring and
explanation layer; the engine is the source of truth for execution, validation, scoring, and
reproducibility. Deterministic evaluators come first; semantic judging is one type among many.

## Document precedence

`docs/design/v2-review-and-v3-plan.md` **wins over** the v2 document wherever they disagree. v2 is
authoritative only for product vision (§1–§6) and even there, §3 personas, §4 journey, and §5
chat/IA were rewritten. When you diverge from v2, note the divergence rather than silently
dropping its reasoning.

## Locked decisions

Decided deliberately. Do not re-litigate without cause; several look independent but are one
mechanism.

1. **Trace capture is an egress proxy**, not in-process instrumentation. The sandbox has no route
   to the internet; a per-run recording proxy is its sole egress and the trace source of truth.
   Provider-agnostic by construction.
2. **The same proxy brokers credentials** — the sandbox holds a dummy key — meters tokens to USD,
   and enforces a hard per-run budget. This is why 1 and 2 are one component.
3. **Multi-tenant schema, single-tenant deploy.** `workspace_id` + Postgres RLS on every table.
4. **LiteLLM is the model abstraction**, embedded as the proxy's outbound leg. **LiteLLM Proxy
   server is never in the run path** — it is an API server, not a forward proxy, so it cannot
   intercept anything, and zero-code-change capture requires interception.
5. **The harness uses no agent framework.** Raw SDKs behind a `ModelGateway`. Two reasons: a
   self-referential hazard (evaluating frameworks you are built on) and dependency conflict with
   the agents under test.
6. **Open source (Apache-2.0), self-hosted, tri-platform.** Linux, macOS, and Windows all run the
   full stack locally; `docker compose up` is the entire quickstart.

Owner decisions already taken: all ten scope cuts accepted; LLM judges are usable-but-provisional
(`UNCALIBRATED` with badges) rather than hard-gated on κ; the hosted example will be a repaired,
versioned copy of the reference agent. **Open: nothing except future implementation choices.**

## Invariants

Each encodes a failure mode found during review. Violating one reintroduces a known bug class.

- **The TLS-interception CA is generated per install and never committed.** A published CA private
  key would let anyone decrypt traffic from every deployment. This is the most dangerous single
  thing to get wrong in this repository.
- **The LLM emits validated JSON, never code and never a string to parse.** Trace rules use a
  closed operator set (`for_all`, `exists_before`, `never`, `within_ms`, …); scalar predicates use
  CEL. No hand-rolled DSL, never `eval()`.
- **`on_missing` and `on_error` have no defaults** — a metric omitting either fails validation. An
  unstated default silently *inverts* safety metrics: an agent that does nothing would score 100%
  on "never refund ineligible orders."
- **Per-case scoring, run-level aggregation, and the gate are separate fields.** Conflating them
  makes two implementers produce different numbers from one spec.
- **Determinism never comes from `temperature: 0`** — sampling params are removed on current
  frontier models, and LiteLLM silently drops `temperature` for Anthropic while OpenAI honours it.
  It comes from structured outputs, fixed cassettes, seeded fixtures, and repeat-and-vote.
- **Repeats and retries are different.** Repeats sample variance; retries mask it. A case passing
  on retry hides the intermittent bug regression detection exists to find.
- **Redaction happens at capture, not display** — otherwise credentials and PII reach the database,
  the vector index, and failure explanations. This applies to harness chat messages too.
- **The in-sandbox shim is compromised-by-default**: a hint, never authority; no secrets; no cost
  or usage claims; the proxy wins every conflict.
- **Fresh container per test case.** Reuse leaks agent state and corrupts results silently.
- **Dashboards render from declarative definitions only** — no arbitrary LLM-generated frontend.
- **Mutating harness tools return explicit states** (`draft`, `validated`, `queued`, `running`,
  `failed`, `rejected`). Never report success you did not observe.
- **Adapter-sourced events are first-class evidence** — tool metrics are scored entirely from them,
  so trace-rule operators must not filter by `source` unless a metric asks.

## Isolation tiers and platforms

Isolation is a tier selected by one `AGENT_RUNTIME` knob, never an architectural fork.

| Tier | Runtime | Platforms | For |
|---|---|---|---|
| 1 (default) | rootless Podman + runc | Linux, macOS, Windows/WSL2 | self-hosters evaluating their own agents |
| 2 (opt-in) | + gVisor `runsc` | Linux only | hardened / running this as a service |
| 3 (future) | per-run microVM | Linux | public multi-tenant, untrusted uploads |

Tier 1 still enforces every boundary not dependent on kernel virtualization. **Docker Desktop on
macOS is rootful-only and cannot host this design** — Podman Machine is required, and the compose
client and in-VM services use different socket paths. Windows runs through WSL2, Linux containers
only, with Windows as client.

## The reference fixture

`fixtures/reference-agent/` holds a real CrewAI + Gemini agent, deliberately unrepaired. It does
not parse, has no entrypoint, registers tools in a runtime dict, mocks with unseeded `random`,
selects its model via an implicit env lookup, and passes a LiteLLM-format model string its own SDK
rejects. See that directory's README for why each defect matters.

**Do not repair it in place.** Any proposed design should be walked end-to-end against it before
being considered done.

## Frontend and UI/UX work — always delegate

**Any task involving user-facing interface goes to the `frontend-engineer` subagent** — components,
layout, animation, gestures, typography, materials, visual polish, and the design decisions behind
them. Do not write UI code in the main thread.

```
Agent(subagent_type="frontend-engineer", prompt="...")
```

| File | Role |
|---|---|
| `.Codex/agents/frontend-engineer.md` | The subagent. Short by design — it delegates to the skill. |
| `.Codex/skills/apple-design/SKILL.md` | House design language: Apple's fluid-interface principles for the web. ~23 KB. |

Two sources, two questions. `apple-design` governs **how it behaves** (motion, gesture,
interruptibility, materials, accessibility) and is always loaded.
[`voltagent/awesome-design-md`](https://github.com/voltagent/awesome-design-md) governs **what it
looks like** (palette, type scale, component styling) and is fetched on demand for visual
calibration — as reference, never as a brand skin to clone. Behavior beats appearance where they
collide. Details are in the agent file.

**Why this split matters for cost.** The design language is large and needed only during UI work.
Keeping it as a skill keeps it out of the main conversation; routing UI work to a subagent loads it
in that agent's separate context, which is discarded on return. Do not inline the skill's content
into this file or the agent file — that undoes the arrangement.

## Working conventions

- Two trace streams must never be conflated: **harness sessions** (what the evaluation agent did)
  and **agent-under-test traces** (what the evaluated agent did). Keep them separate in schemas,
  storage, and UI.
- Prefer extending `docs/design/` over creating top-level documents.
- This is a public repository: no telemetry, no phone-home, no bundled credentials.
