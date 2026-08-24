# LLM Agent Evaluation Engine

A configurable evaluation engine for agentic systems.

Connect an agent codebase; the platform parses it, helps you translate natural-language evaluation
intent into a structured specification, runs the agent in an isolated container against test cases,
captures full execution traces, scores them with configurable evaluators, and explains every result
with links back to the evidence that produced it.

**The core thesis: this is not an LLM-as-a-judge product.** The LLM authors and explains; a
deterministic engine executes and scores. Semantic judging is one evaluator type among many —
exact match, schema, rule, trace, numeric, reference, and custom code come first, because most of
what teams actually need to check can be stated exactly.

## Status: design phase

**There is no code yet.** This repository currently holds the design specification and the review
that produced it. Nothing is buildable or runnable.

| Document | What it is |
|---|---|
| [`docs/design/v2-review-and-v3-plan.md`](docs/design/v2-review-and-v3-plan.md) | **Start here.** A review of v2 — load-bearing gaps, internal contradictions, missing evaluation categories, operational landmines — plus the specification of what v3 must contain and the decisions taken. |
| [`docs/design/LLM_Agent_Evaluation_Engine_v2.md`](docs/design/LLM_Agent_Evaluation_Engine_v2.md) | The original 44-section design. Good on product vision (§1–§6); **superseded on technical mechanisms** wherever the review contradicts it. |
| [`fixtures/reference-agent/`](fixtures/reference-agent/) | A real, deliberately unrepaired CrewAI agent used as the design's reality check. |

The next deliverable is `docs/design/LLM_Agent_Evaluation_Engine_v3.md`.

## Design at a glance

- **Traces come from an egress proxy.** The run sandbox has no route to the internet; a recording
  proxy is its sole egress and the source of truth for what happened. This works on any framework
  with zero changes to user code, and the same component brokers credentials, meters cost, enforces
  a hard per-run budget, replays cassettes, and injects faults.
- **The agent's own tool calls are recovered by an in-sandbox shim** — framework-native hooks where
  they exist, import-hook symbol wrapping where they don't. The shim runs inside the untrusted
  boundary, so it is treated as a hint and never as authority; the proxy wins every conflict.
- **The LLM emits validated JSON, never code.** Trace rules use a closed operator set; scalar
  predicates use CEL. Nothing generated is parsed as a string or evaluated.
- **Multi-provider by default** via LiteLLM. The platform's own calls and the agent under test are
  separate concerns — the platform never dictates what model your agent uses.
- **Isolation is tiered**, not fixed: rootless containers everywhere, gVisor opt-in on Linux for
  anyone running this as a service.

## Platform support

Linux, macOS, and Windows all run the full stack locally; every platform runs Linux containers.
macOS uses Podman Machine (Docker Desktop is rootful-only and cannot host the rootless design);
Windows runs through WSL2 with Windows as the client only.

## Security note for self-hosters

The egress proxy terminates TLS, which requires a CA that agent images trust. **That CA is
generated per install on first run and is never committed to this repository.** A published CA
private key would let anyone decrypt traffic from every deployment.

The project sends no telemetry and bundles no credentials. You bring your own provider keys.

## License

[Apache License 2.0](LICENSE).
