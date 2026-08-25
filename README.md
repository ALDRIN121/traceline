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

## What's inside

- **EvaluationSpec** — one validated JSON document defines cases, metrics, scoring, aggregation,
  and the run-level gate. An LLM harness (opt-in) translates intent into a spec; the engine never
  executes LLM output.
- **Trace capture by egress proxy** — the agent's sandbox has no internet route; a per-run
  recording proxy is its sole egress and the trace source of truth. Provider-agnostic by
  construction, and the same proxy meters token cost to USD and enforces a hard per-run budget.
- **Deterministic-first evaluators** — exact match, schema, numeric, trace rules, CEL predicates,
  reference answers, and semantic judging (marked `UNCALIBRATED` until validated).
- **Runs with a real lifecycle** — queued → running → aggregating → complete; cancel is
  non-blocking, interrupted runs resume, and each test case runs in a fresh process so state never
  leaks between cases.
- **Evidence-linked dashboards** — a declarative definition renders run overviews, per-case
  tables, and trace evidence: every score is clickable down to the event that produced it.
- **Self-hosted and clean** — Apache-2.0, no telemetry, no phone-home, no bundled credentials;
  runs fully locally on Linux, macOS, and Windows/WSL2.

## Quickstart

```bash
pip install -e .
eval-engine serve                 # dashboard at http://127.0.0.1:8000
```

Create a project, pass the smoke gate, and run an evaluation:

```bash
eval-engine init my-agent-eval                       # spec template + project skeleton
eval-engine smoke my-agent-eval                      # smoke gate: does the agent actually run?
eval-engine run my-agent-eval/spec.json --entrypoint python my-agent-eval/agent.py
eval-engine results <run-id>                         # scores, gate status, revisions
```

The dashboard is served by the same server as `eval-engine serve` — run selector, metric table,
per-case table, and clickable trace evidence.

## The reference fixture

[`fixtures/reference-agent/`](fixtures/reference-agent/) holds a real, deliberately unrepaired
CrewAI + Gemini agent. The smoke gate must FAIL it (and does) — the fixture exists so every design
proposal and every engine change is walked against a genuinely broken agent before being accepted.
[`fixtures/sample-agent/`](fixtures/sample-agent/) is the well-formed counterpart: a small
support-triage agent that runs offline or against any OpenAI-compatible endpoint.

## Design

The full design lives in [`docs/design/`](docs/design/). Start with the
[review-and-v3 plan](docs/design/v2-review-and-v3-plan.md), then the
[v3 specification](docs/design/LLM_Agent_Evaluation_Engine_v3.md) — the repo is implemented
against v3, and section references in code (e.g. `§11A`) point at it.

## Tests

```bash
PYTHONPATH=src python -m pytest -q    # 508 tests, no network required
```

A few tests are marked `live`: they exercise a real LLM provider through `DEEPSEEK_API_KEY` in
`.env` (never committed) and are skipped by default — run them last.

## License

Apache-2.0. See [LICENSE](LICENSE).
