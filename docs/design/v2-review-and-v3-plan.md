# Plan — Harden the LLM Agent Evaluation Engine design into v3

## Context

`~/Downloads/LLM_Agent_Evaluation_Engine_Design_Doc_v2.md` (44 sections) specifies a
conversational, configurable evaluation engine for agentic systems. Its **product thesis is
strong and carries into v3 largely unchanged**: evaluation is a configurable engine rather than
an LLM-judge wrapper; the LLM authors and explains while the engine executes and scores; every
score traces to evidence. The domain model (Project → Workflow → Evaluation → Metric →
Evaluator → TestCase → Run → Trace) is sound, and §43's decision table is a good set of calls.

The problem is not the vision — it is that **the mechanisms the vision rests on are named but
never specified.** Two independent reviews of the doc (mine and an adversarial completeness
pass) converged on the same conclusion: an engineer handed v2 could not build the runner, the
tracer, the trace-rule evaluator, or the judge without inventing the design first. And the
riskiest component of all — how traces are captured from arbitrary uploaded code — does not
appear anywhere in the document.

`~/Developer/llm_agent_eval/` is empty. **This session produces Design Doc v3**: v2 with the
gaps closed, the contradictions resolved, the missing evaluation categories added, and the
contracts specified concretely enough to build from. No implementation sequencing (a later
session), no code.

### Deliverables

| File | Contents |
|---|---|
| `~/Developer/llm_agent_eval/docs/design/LLM_Agent_Evaluation_Engine_v3.md` | The hardened specification. Keeps v2's section numbering so the two diff cleanly; new material takes suffixed numbers (§12A etc.). |
| `~/Developer/llm_agent_eval/docs/design/v2-review.md` | Part 1 of this plan as a standing record — the gaps, contradictions, missing use cases, and landmines that justify each v3 decision. Kept beside v3 so future readers can see *why*, not just *what*. |

Also `git init` the project so both documents are tracked from the start — the design will churn,
and the rationale record is only useful if its evolution is visible.

**Reality check used throughout.** `~/Downloads/multi_agent_system.py` is a real CrewAI +
LangChain-Gemini manager/sub-agent system. It breaks v2 in **six** places before evaluation even
begins — the last two found only by reading the dependency's actual source:

1. **It does not parse.** The file is truncated mid-method inside `_create_tasks`, so it is a
   `SyntaxError` — not merely entrypoint-less. Ingestion must gate on parse *before* dependency
   install, and "uploaded code does not parse" is its own guided recovery state.
2. **No runnable entrypoint** — nothing calls `Crew(...).kickoff()`.
3. **Zero instrumentation hooks** — no callbacks, tracers, or handlers anywhere.
4. **Tools hand-mocked** with `time.sleep(random.uniform(...))` and unseeded `random`.
5. **Model selected via an implicit `GOOGLE_API_KEY` lookup** — a source hash does not tell you
   what actually ran.
6. **Its model string is invalid for its own SDK.** It passes `model="gemini/gemini-2.0-flash"` —
   a *LiteLLM* convention — to `langchain_google_genai`. Verified against the 4.3.5 wheel source:
   nothing strips a `gemini/` prefix, so the first LLM call likely 400s. The smoke gate must
   classify this as **runtime misconfiguration**, not infrastructure failure, or the error is
   misattributed to the sandbox.

v3 must answer all six. v2 answers none. A hosted "try the example" fixture must therefore ship a
**repaired, versioned** copy, explicitly labelled as repaired.

---

## Decisions locked this session

| # | Decision | Consequence |
|---|---|---|
| 1 | **Deliverable is Design Doc v3 only** | Hardened spec, schemas, use cases. No build sequencing, no code. |
| 2 | **Trace capture = egress proxy, OTel as enrichment** | New §12A. The sandbox has no direct egress; a recording proxy is the trace source of truth. |
| 3 | **Multi-tenant schema, single-tenant deploy** | `workspace_id` + Postgres RLS on every table from day one; ships as one Compose stack. |
| 4 | **Agent credentials brokered at the proxy** | Sandbox holds a dummy key; the proxy substitutes the real one, meters cost, enforces budget. |
| 5 | **LiteLLM is the model abstraction** | Multi-provider by default for the platform's own harness and judge calls, and for cost accounting across providers. No provider lock-in. |
| 6 | **Open source, self-hosted, tri-platform** | Linux, macOS, and Windows must all run the full stack locally. `docker compose up` is the entire quickstart. Isolation becomes a **tier**, not a fork (§4K). |

Decisions 2 and 4 are one mechanism, which is why the proxy is v3's keystone: a single component
delivers tracing, cost metering, secret isolation, record/replay, and fault injection. It is
provider-agnostic by construction — required here, since the reference agent calls **Gemini**.

Decision 5 interacts with that keystone directly, and the exact topology is under review:
**LiteLLM ships both an SDK and a proxy server**, and its proxy already provides multi-provider
routing, virtual keys, cost tracking, budgets, and callback hooks — a large fraction of what §12A
needs. The open question is whether LiteLLM Proxy *becomes* the egress proxy, or sits behind a thin
custom one owning what LiteLLM does not cover: canonical trace fidelity, record/replay cassettes,
fault injection, and non-LLM HTTP traffic. This resolves in v3, before any code is written.

Two LLM surfaces must never be conflated, and v3 names them separately:

- **Platform calls** — the harness agent and the LLM judge. LiteLLM governs these directly, and
  multi-provider support means judge comparability across providers becomes a real problem (§15A).
- **Agent-under-test calls** — whatever the evaluated agent uses (Gemini via
  `langchain_google_genai` in the reference agent). The platform does **not** choose these; the
  proxy observes and meters them regardless of provider or SDK.

---

## Part 1 — Review of v2

### 1A. Load-bearing gaps — asserted but never specified

| # | §  | Missing | What breaks |
|---|---|---|---|
| **G1** | 12 | Trace event *types* are listed; the **capture mechanism is never named**, nor are per-type payload schemas. | Nothing produces traces. Every trace/tool metric, the evidence layer, and most dashboard components are unimplementable. |
| **G2** | 11, 35, 37.6 | **The agent invocation contract.** How a test input reaches the agent (stdin? env? file? HTTP?), how the final response returns, how a ZIP becomes an image, base images, dependency install, build-time network policy, cache keys. `invoke()`/`collect_trace()` are method names, not a contract. | The Docker runner cannot be built. §38's "run 500 cases" has no defined starting point. |
| **G3** | 19, 11, 10 | **No mock/fixture world.** Test cases carry `expected.tool_output` — deterministic tool results only a mock can deliver — yet network is off by default and no service virtualization exists. | The doc's own refund example cannot run. With network on, results are flaky and side effects are real; with it off, most agents fail to start. Confirmed by the CrewAI sample, whose author hand-wrote mocks. |
| **G4** | 21, 20 | **No variance model or comparability rules.** `92.1% → 86.4%` is treated as fact; `max_allowed_regression: 0.05` has no statistical basis; nothing says what makes two runs comparable (same dataset version? case intersection?). | Agents are stochastic. Teams chase noise and miss real regressions. Note `temperature: 0` is *not* an available escape hatch — sampling params are removed on current frontier models. |
| **G5** | 8C, 9 | `every(refund_order) has prior(check_refund_eligibility)` and `args.order_id == expected.order_id` are **an undefined language and arbitrary code**. What does `prior` mean under concurrent calls? What does `every` quantify? | The one genuinely novel component in the doc is a free-text field. Someone writes a parser, or reaches for `eval()` — a sandbox escape. |
| **G6** | 7, 13 | **Missing-target semantics undefined.** Metric targets `tool_output` of `refund_order`; the agent never calls it. PASS (vacuously true), FAIL, or SKIPPED? | Silently *inverts* safety metrics — an agent that does nothing scores 100% on "never refund ineligible orders." |
| **G7** | 7, 13 | **Per-case scoring conflated with run-level aggregation**, and `ERROR` cases have no defined rollup treatment. | Two implementers produce different numbers from the same spec; error handling alone moves headline metrics by points. |
| **G8** | 32, 35 | **No "can this agent even run?" gate** — dependency install, interpreter version, env vars, entrypoint existence all assumed. | The reference agent has *no entrypoint at all*. v2's only path is to fail. Users author metrics against an agent that was never executable. |
| **G9** | 7, 13, 18 | **Run-scoped metrics have no storage path.** §7 allows target type "evaluation run" and §13 shows "Latency p95 — Run metrics", but `MetricResult` is keyed by `test_case_id` and the table is "per-test". | Run-level metrics and §21's failure budgets cannot be persisted or gated. |
| **G10** | 20, 32 | **Reproducibility anchors are incomplete.** ZIP ingestion has no commit; images aren't digest-pinned; and an **env-var-selected model** means the source hash lies about what ran. Harness authoring provenance (which model wrote a spec) is unrecorded. | §2's "every run identifies the model versions involved" is false for the common env-configured agent — including the reference one. |
| **G11** | 14, 35.1 | **The mock preview has no generation mechanism.** Synthetic results produced by what? Yet Preview gates Approval. | Users set real thresholds against fabricated distributions the doc simultaneously insists are "not a prediction." |
| **G12** | 7, 15 | **The LLM judge has no contract**: `rubric_ref` is never schematized, no input/output shape, no calibration, no variance handling, no caching. `range: [0,1], threshold: 0.85` assumes a scale never defined. | One of five MVP evaluator types cannot be built as written. |

### 1B. Internal contradictions

| §§ | Conflict |
|---|---|
| 19 ↔ 11 | "Never expose platform credentials to user containers" vs. an agent that must call a provider to run. *(Resolved by decision 4.)* |
| 14 ↔ 37.3 | **Two different, unmerged dashboard registries.** Coverage matrix / Regression alert / Insight panel exist only in §14; ToolCallInspector / WorkflowGraph / ApprovalPanel / **HTMLPreview** only in §37.3. The registry is the doc's own defense against "arbitrary frontend injection" (§37.1) — and `HTMLPreview` punches a hole straight through it. |
| 18 ↔ 24 | `trace_events` as a plain table vs. "must support high event volume without coupling dashboard latency to raw event scans." The NFR states the requirement; no mechanism delivers it. |
| 38 ↔ 31.4, 35.3, 11 | The flagship walkthrough runs **500 cases in parallel with no approval** — violating §31.4's mandatory confirmation before running user code at scale, and §35.3's own `concurrency: 20`. |
| 3 ↔ 29, 26, 41 | **"Production replay" is listed as a core use case** but its only enabling mechanism (§29's monitoring bridge) is future, absent from MVP scope and the roadmap. |
| 26 ↔ 41 | MVP criteria demand "a real agent repository" executes; §41 ships **one canonical Python adapter**. Most real repos parse but cannot run — the acceptance test fails by construction. |
| 29 ↔ 15 | Human review queues are "future," yet the LLM judge in core has no other source of ground truth. |
| 20 ↔ 35.2 | "Immutable references to all meaningful inputs" vs. "select cached image" with no digest pinning or cache-invalidation key. |

### 1C. Missing evaluation use cases

v2's §3 table covers the deterministic core well. These are absent, or wrongly deferred:

| # | Category | Why it belongs in v3 |
|---|---|---|
| **U1** | **Multi-turn / conversational** | Every test case is single-shot (`input: {query: ...}`). Needs multi-turn cases, a **user-simulator**, goal completion across a dialogue, turn vs conversation metrics. |
| **U2** | **Human-in-the-loop / approval gates** | The doc's *own* example workflow is "eligibility → approval → action", yet nothing can test that the agent pauses, presents correct options, respects the decision, and resumes. No interaction script in §10, no `wait_for_input`/`user_response` events in §12. |
| **U3** | **Security & prompt-injection resistance** | v2's "adversarial" means *edge cases*. Missing: injection and jailbreak resistance, unauthorized tool invocation under injection. |
| **U4** | **Agent data discipline (PII echo)** | Distinct from platform-side redaction: does the agent echo PII, leak it into tool args, or fail to redact? |
| **U5** | **RAG / retrieval quality** | Groundedness, context relevance, citation accuracy, recall@k. `retrieval` is **missing from the canonical event list** entirely. |
| **U6** | **Multi-agent delegation** | Deferred to "future," yet the reference agent *is* a manager/sub-agent crew. Handoff correctness, delegation choice, per-sub-agent attribution, shared-state consistency. |
| **U7** | **Tool-error recovery / resilience** | Behavior under 429s, 5xx, timeouts, malformed JSON, 10× slowdowns — retry with backoff, degrade, or fabricate? The proxy makes fault injection nearly free. |
| **U8** | **Termination, loop safety, tool parsimony** | Infinite loops, oscillation between two tools, "called the same tool 14 times", step blowups, budget exhaustion. How agents most often fail in production. |
| **U9** | **Run-to-run stability** | "Passed in ≥9 of 10 runs" is inexpressible; §13 stores one point estimate per case. Prerequisite for G4's regression semantics. |
| **U10** | **Pairwise / side-by-side preference** | A/B two versions on identical cases with a preference judge — the standard way judge-based eval is actually done. Absent. |
| **U11** | **Streaming behavior** | No token-stream, first-token, or partial-output events; no time-to-first-token metric. Streaming-first agents are unrepresentable. |
| **U12** | **Cost & token efficiency** | Latency is tracked; cost appears only as a *risk*. Needs per-case cost, cost-per-successful-task, cost regression gates. |
| **U13** | **Human annotation & labeling** | Move from "future" into core: the only ground-truth source for calibrating U3 and the judge. |
| **U14** | **Continuous evaluation / CI gating** | Deferred to Phase 9, but §1's primary outcome *is* regression detection — which only means something when runs fire automatically on new commits. |

### 1D. Operational and scale landmines

Beyond §28's risk table:

- **Secrets and PII enter traces at capture.** Credentials in tool args and headers, prompts with user PII in `llm_call` payloads — then re-surfaced by failure explanations (§22) and dashboards, and **indexed into pgvector** (§18), which will happily embed hardcoded keys. Encryption at rest does not help. Redaction must happen *at capture*, not at display.
- **Unbounded payload growth.** ~500 cases × ~100 events × ~10KB ≈ 0.5 GB *per run*, before evidence linking reads any of it. No caps, partitioning, retention, or compaction anywhere.
- **Container lifecycle is undefined, and both options are wrong as-is.** Reuse leaks agent state between cases (silent result corruption); fresh-per-case at 10k means image-pull storms and bridge-network IP exhaustion. No orphan reaping on worker crash, no cancellation semantics for §35.4's `CANCELLED`, no "incomplete" marker — dashboards will render half-aggregated runs as complete.
- **Retries vs repeats are conflated.** §11 lists retries but never says which attempt is authoritative. A case that passes on retry 2 hides exactly the intermittent bug regression detection exists to find.
- **Judge calls sit on the critical path.** §35.4 puts `SCORING` inside the per-case lifecycle, serializing the run's tail; the per-case `timeout_seconds: 120` can expire while scoring still owes judge calls.
- **Docker seams are the escape vector.** Socket-mounted Docker or DinD is root-equivalent. Custom evaluators run either in the agent's container (seeing its credentials) or on the worker (untrusted code in the control plane) — both unacceptable, neither decided.
- **Archive handling is under-defended.** §19 scans for path traversal only — no decompression-bomb, symlink, file-count, or extracted-size caps. Dependency installation executes untrusted code at build time.
- **Image build is a standing outage source.** No base-image pinning, no cache key, no stale-image GC, and an unresolved dilemma: offline builds break most agents, online builds open the supply chain.

---

## Part 2 — What Design Doc v3 will specify

The sections below are the substance of `docs/design/LLM_Agent_Evaluation_Engine_v3.md`.

### §12A — Trace capture: the egress proxy *(closes G1)*

The keystone. The run sandbox gets **no route to the internet**; a per-run recording proxy is the
sole egress and the trace source of truth.

```
run container  (no egress, dummy API key)
   └── HTTPS ──▶ per-run proxy ──▶ real provider
                   ├── decode request/response → canonical TraceEvent
                   ├── substitute real credential      (decision 4, closes §19↔§11)
                   ├── meter tokens → USD → hard budget, abort on breach
                   ├── redact at capture                (secrets/PII never persisted)
                   ├── record / replay cassette         (§10A)
                   └── fault injection                  (U7)
```

- **Provider-agnostic**: decoders for Anthropic, OpenAI, Google (the reference agent's provider),
  Bedrock, Vertex; unknown hosts still traced generically.
- **Zero user code changes** — which is what makes "connect a repo and go" an honest promise.
- **OTel/OpenInference enrichment is optional and additive**, injected via `sitecustomize.py` to
  recover in-process detail. It executes *inside* the untrusted boundary, so it is a hint, never
  authority. The proxy always wins on conflict.
- v3 states the boundary plainly: **local, non-HTTP tool calls are invisible to the proxy** and
  need adapter or OTel enrichment. Stated as a known limitation, not hidden.

### §12B — Event schemas, caps, and redaction

Per-type payload schemas (`llm_call` carries messages/tokens/model; `tool_call` carries args;
etc.), plus a size cap with overflow to object storage via `payload_ref`. New event types:
`retrieval`, `delegation`, `guardrail_check`, `wait_for_input`, `user_response`, `stream_start`,
`first_token`, `retry`, `budget_exceeded`. Event shape gains `workspace_id`, `repeat_index`,
`attempt`, `payload_ref`, `redaction_state`, a `cost` block, and OTel `trace_id`/`span_id`.
Concurrency semantics for `parent_event_id` are defined for parallel tool calls.

### §11A — Invocation protocol and image build *(closes G2)*

The contract v2 never wrote down. A single documented protocol — test input delivered as JSON on
a mounted path, final response and structured result written back to a declared output path,
exit-code semantics, per-case timeout — plus an image-build spec: pinned base images per runtime,
dependency install from detected manifests, a **cache key** derived from
`(base_image_digest, manifest_hash, adapter_version)`, and an explicit build-time network policy
(allowlisted package index, never open egress).

### §11B — Container lifecycle, concurrency, cancellation

**Fresh container per case** (state leakage silently corrupts results and is not worth the
savings), with a warm image cache and a pool cap to avoid pull storms and IP exhaustion. Orphan
reaping on worker crash, real cancellation semantics for in-flight containers, and an
`INCOMPLETE` run marker so partially-aggregated runs never render as finished.

### §11C — Determinism, repeats, and statistical validity *(closes G4)*

```yaml
execution:
  repeats: 5                      # independent samples; distinct from retries
  retry: { max: 2, authoritative: first_attempt }   # retries never mask flakiness
  flakiness: { classify: true }   # 0 < pass_rate < 1  ⇒  FLAKY, reported separately
regression_policy:
  - metric: refund_amount
    max_allowed_regression: 0
    significance: 0.95            # bootstrap CI; deltas inside the noise band aren't regressions
comparability:
  require_same: [evaluation_version, dataset_version]
  on_mismatch: compare_intersection_and_warn
```

v3 states the correction explicitly: **determinism cannot come from `temperature: 0`** —
sampling parameters are removed on current frontier models. It comes from structured outputs,
fixed cassettes, seeded fixtures, and repeat-and-vote. Runs report confidence intervals, not bare
percentages, and separate *flaky* from *failing* — different bugs, different fixes.

### §9A — Predicate and trace-rule languages *(closes G5)*

**The LLM emits validated JSON — never a string to parse, never code to execute.**

- **Scalar predicates → CEL** ([Common Expression Language](https://cel.dev/overview/cel-overview)):
  a real grammar, non-Turing-complete, explicitly designed for safe evaluation of untrusted
  expressions, with a maintained pure-Python implementation
  ([`cel-python`](https://pypi.org/project/cel-python/)). Replaces v2's `rule.expression`.
- **Trace rules → a closed JSON operator set**: `for_all`, `exists`, `never`, `exists_before`,
  `exists_after`, `immediately_precedes`, `count`, `within_ms`, `and`, `or`, `not`, `implies` —
  each with defined semantics under concurrency.

```yaml
evaluator:
  type: trace_rule
  rule:
    op: for_all
    match: { event: tool_call, tool: refund_order }
    assert:
      op: exists_before
      match:
        event: tool_result
        tool: check_refund_eligibility
        where: "payload.output.eligible == true"   # CEL
```

Three properties follow for free: it is schema-validatable (so structured outputs with
`strict: true` can generate it), there is no parser to write, and **every operator reports the
events it matched — which is §12's evidence link, generated rather than hand-wired.**

### §7A — Corrected metric semantics *(closes G6, G7)*

```yaml
- id: refund_amount
  target:
    type: tool_output
    tool: refund_order
    selector: "$.amount"
    occurrence: last              # first | last | all | index:N
    on_missing: fail              # fail | skip | pass      ← G6, was undefined
  evaluator: { type: numeric, expected: "test.expected.refund_amount", tolerance: 0 }
  scoring:   { type: binary, range: [0, 1] }                # per CASE
  aggregation:                                               # per RUN  ← G7, was conflated
    method: pass_rate                                        # pass_rate|mean|p50|p95|min|sum
    on_error: fail                                           # fail | exclude
  gate:      { min: 1.0 }                                    # v2's "threshold"
  weight:    1.0
```

`on_missing` and `on_error` have **no defaults**; validation rejects a metric omitting either.
Silent defaults are how safety metrics invert.

### §13A — Run-scoped results and a decoupled scoring phase *(closes G9)*

A `run_metric_results` table for run- and dataset-scoped metrics (latency p95, failure budgets,
cost totals), separate from per-case results. **Scoring moves out of the per-case lifecycle** into
its own asynchronous phase, so judge latency no longer serializes the run tail or races the
per-case execution timeout.

### §14A — Preview generation and one merged registry *(closes G11, §14↔§37.3)*

The two component vocabularies merge into a **single registry**. **`HTMLPreview` is deleted** —
preview renders through the same declarative renderer with synthetic data and a persistent mock
banner. Synthetic results are generated **deterministically from the spec's own declared
expectations and ranges** — never LLM-invented distributions — and the preview surface refuses to
display anything resembling a predicted score.

### §15A — The judge as a validated instrument *(closes G12)*

- **`Rubric` becomes a first-class versioned entity** with an explicit judge I/O contract
  (score + justification + evidence refs) and a **calibration set** of human-labeled examples —
  which is why U13 moves into core.
- A **judge-agreement metric** (Cohen's κ against human labels) gates whether a judge may be used
  at all, recomputed when the judge model version changes.
- Bias controls: position swapping for pairwise (U10), length-normalization checks, few-shot
  anchoring drawn from the calibration set.
- Judge model ID **and** rubric version pinned into the run manifest.
- **Cost**: judge calls batched through the Message Batches API at 50%; verdicts cached on
  `(rubric_version, judge_model, content_hash)` so identical traces are never re-judged.

**Model policy** (new subsection):

| Role | Model | Rationale |
|---|---|---|
| Harness agent | `claude-opus-5`, adaptive thinking, `effort: high` | Reasoning over code and spec authoring. Prompt caching on the stable project-context prefix, verified via `cache_read_input_tokens`. |
| LLM judge | `claude-opus-5` + `output_config.format` | Determinism from schema, not temperature. Batched. |
| Bulk/mechanical (cluster labels, test drafts) | `claude-haiku-4-5` | ~5× cheaper; sufficient for draft-then-review. |

Plus a **pre-run cost estimate shown before execution**, a hard per-run budget enforced at the
proxy, per-workspace quotas, and spend split three ways — harness / agent-under-test / judge —
because they have different owners.

### §10A — The world model: fixtures, cassettes, fault injection *(closes G3)*

Modes `live | record | replay | hybrid`, per-case tool mocks, and fault injection (which delivers
U7 directly). This is also what makes "production replay" achievable rather than aspirational.

```yaml
world:
  mode: replay
  cassette: cassettes/refund_v3
  tool_mocks:
    - { tool: charge_payment, when: "args.order_id == '123'",
        returns: { status: refunded, amount: 49.99 } }
  fault_injection:
    - { tool: get_order_status, on_case_tag: tool_failure, raise: timeout }
```

### §32A — Runtime preparation and the smoke gate *(closes G8)*

A lifecycle stage before any evaluation design is permitted:

```
INGESTED → ANALYZED → RUNTIME_PREPARED → SMOKE_PASSED → EVALUABLE
```

Discovery emits **confidence scores with provenance**, and when no entrypoint is found — the
reference agent's exact situation — the product asks the user to declare one or supply a small
harness shim. **A supported path, not an error state.** One successful invocation is required
before metric authoring unlocks.

### §37A — Adapter contract

Baseline is **generic Python + proxy trace**, which works for CrewAI, LangGraph, or a bare
`while` loop. Framework adapters only *enrich*; none is required for correctness — which
dissolves the §26↔§41 contradiction.

```python
class AgentAdapter(Protocol):
    def discover(self, tree: SourceTree) -> DiscoveryReport: ...   # + confidence, provenance
    def prepare(self, ctx: RuntimeContext) -> ImageRef: ...
    def smoke(self, ctx: RuntimeContext) -> SmokeResult: ...
    def invoke(self, case: TestCase, ctx: RuntimeContext) -> Invocation: ...
    def collect(self, inv: Invocation) -> list[TraceEvent]: ...    # enrichment only
```

### Revisions to existing sections

| § | Change |
|---|---|
| 3 | Use-case table extended with U1–U14. |
| 17 | Idempotency keys on run creation (double-submit = double spend); cancellation semantics; SSE progress instead of polling; pagination; error envelope. |
| 18 | `workspace_id` + RLS on every table; object-storage key scheme per tenant. `trace_events` partitioned by run, payloads offloaded past a cap, dashboards read pre-aggregated results only, retention TTL. New tables: `workspaces`/`users`/`memberships`/`api_keys`, `rubrics`/`rubric_versions`/`rubric_calibration_labels`, `cassettes`, `run_case_attempts`, `run_metric_results`, `cost_ledger`, `annotations`, `audit_log`. Referenced datasets/evaluations protected from GC. |
| 19 | Credential brokering; **rootless Podman or a dedicated gVisor runner pool — never the Docker socket**; custom evaluators in their own sandbox tier, isolated from both agent container and worker; archive defenses (decompression bombs, symlinks, file-count and extracted-size caps); redaction pipeline at capture; retention and deletion. Audit **reads**, not just writes. |
| 20 | Agent version = content-addressed `source_digest` (Merkle hash of the normalized tree) **plus resolved runtime config** — env-selected model, image digest, dependency lock — since the source hash alone lies. Harness authoring provenance recorded per spec version. |
| 21, 26 | CI gating and continuous evaluation (U14) promoted from Phase 9 into the core loop. |
| 38 | Walkthrough corrected to honor §31.4 confirmation and §35.3 concurrency, with the cost estimate shown before the user approves the run. |
| 40 | Platform self-observability (API and worker metrics/logs/traces), distinct from harness sessions and agent-under-test traces. |

---

---

## Part 3 — The agent layer (§31–§37)

Parts 1 and 2 scrutinized the evaluation engine. §31–§37 — the conversational harness, project
parsing, search, use-case discovery, spin-up, the tool registry, and UI/plugin generation — carry
the same class of problem: **the architecture diagrams name components that are never defined.**

### 3A. Agent-layer gaps

| # | § | Missing | What breaks |
|---|---|---|---|
| **A1** | 31.2 | **The "Harness Planner" sub-components are never defined as anything.** Are Project Context Retriever / Workflow Reasoner / Evaluation Designer separate LLM calls, prompts, subagents, or router branches? "May use one or more LLM calls internally" is a shrug. | This is the single largest agent decision — it sets cost, latency, debuggability, and whether state can be handed between phases. Unanswerable as written. |
| **A2** | 31, 6, 18 | **No conversation persistence model.** "Maintain context across iterations" is a responsibility, but there is no session or message entity in the domain model or schema. §40 stores sessions for *audit*, not as working state. | Sessions cannot resume. A refresh, a timeout, or an LLM outage loses in-progress evaluation design. |
| **A3** | 31.3 | Context layers 0–4 have **no token budget, eviction policy, or compaction strategy**, and **no prompt-caching design** — despite layers 0–2 being an obviously stable prefix re-sent every turn. | Long design sessions overflow the window. Cost is several times higher than necessary on the platform's most frequent operation. |
| **A4** | 31.4, 36.2 | **Confirmation policy is prose.** `ToolDefinition.confirmation_policy` exists with no defined values, and nothing says whether it is static per tool or dynamic on arguments (10 cases vs 10,000). | §38's walkthrough already violates it. Without argument-sensitive policy, either everything prompts or nothing does. |
| **A5** | 31, 39 | **No transactional model for multi-step mutations.** Agent creates an evaluation, then 3 metrics, then fails. §39 covers *logical* failures but not orphaned drafts, context overflow mid-task, fabricated object IDs, or tool-call loops. No idempotency: a retried tool call creates a second evaluation. | Silent data corruption in the object graph. |
| **A6** | 32.2 | **Ingestion is drawn as a linear pipeline but takes minutes.** No job model, no progress surfacing, no partial availability. | The user watches a spinner with no idea whether it is working. |
| **A7** | 32.2 | **"LLM semantic analysis" over an arbitrary repo has no scoping or cost bound.** Which files are sent? How many? What budget? | A 5,000-file repo cannot be analyzed wholesale. Undefined = unbounded cost. |
| **A8** | 32.4 | **The Project Model asserts `agents[]`, `workflows[]`, `tools[]` as facts**, but static analysis of dynamic Python routinely fails. The reference agent builds its tool registry **at runtime in a dict** — a pure-AST parser sees a method returning a dict, not three tools. | The knowledge graph is confidently wrong, and everything downstream inherits the error. |
| **A9** | 32.6 | Incremental re-parse invalidates "affected nodes" — but **no inter-file dependency graph exists**, so "affected" is undefined. | Change a base class; downstream analysis stays stale and silently wrong. |
| **A10** | 32.3 | Env-var *references* are extracted "without exposing secret values" — but **a `.env` in the ZIP contains real secrets**, which land in the file inventory, search index, and pgvector. | Credential leak by default. |
| **A11** | 33.5 | Hybrid retrieval names lexical + structural + embeddings but **no fusion/ranking strategy**, and **no code-chunking strategy** (by symbol? file? overlap?). | Chunking alone determines retrieval quality; unspecified means unbuildable and untunable. |
| **A12** | 33 | **No result-size budgets.** `read_file(path, range)` has no cap; `search_*` has no result limit. | One call pulls a 10k-line file into context and blows the window. |
| **A13** | 33.2 | `search_traces` "when permitted" — **permission model undefined**; §33.3 forbids crossing workspace boundaries but no mechanism enforces it. | Cross-tenant retrieval leak, invisible to §19's write-only audit. |
| **A14** | 34.5 | **Use-case candidates have no ranking, grouping, dedup, or cap.** A 20-node workflow yields dozens of cards. | This is precisely where §28's own "metric sprawl" risk materializes — and §34 has no defense. |
| **A15** | 34.4 | `business_importance` and `risk_level` are **LLM guesses with no grounding** and no "inferred, unverified" marking. | The model does not know the business. Guessed priorities steer real evaluation effort. |
| **A16** | 34 | **No coverage model** — nothing computes which workflow branches, tools, or error paths have *no* use case. | The most valuable thing discovery could report is the thing it does not report. |
| **A17** | 10, 31 | **No conversational test-case authoring flow.** §10 lists dataset *sources*, but not what happens when a user says "add a case where the order is already refunded." | The core chat interaction for building datasets is undesigned. |
| **A18** | 35.4 | **No queue model** (priority, fairness, backpressure), **no partial results** (AGGREGATING is a discrete phase), **no resume** of a failed run. | Ten users × 5k cases starves everyone. Three failed cases force a 5k re-run. |
| **A19** | 36.3 | **Registry subsetting has no algorithm.** "The harness receives a subset appropriate to the current task" — chosen how? No correlation ID joins session → tool call → mutation → run. | Too few tools and the agent cannot act; too many and selection accuracy degrades. Audit cannot reconstruct a causal chain. |
| **A20** | 37.3 | **The dashboard definition schema does not exist.** The registry lists component *names*; there is no layout model, **no data-binding language**, and no interaction/filter propagation. | Exactly parallel to G5: the novel artifact the LLM must emit is undefined. The renderer cannot be built. |
| **A21** | 37.5 | **Plugin security is absent.** `permissions[]` is unenumerated; UI component packs are **arbitrary frontend code rendering inside the app** — the precise thing §37.1 exists to prevent. No sandboxing, review, signing, or compat enforcement. No migration when a registry component is removed. | The plugin system reintroduces the risk the constrained-UI design was built to eliminate. |

### 3B. What v3 specifies for the agent layer

**§31A — Harness topology.** *One conversational agent* with phase-scoped tool subsets, plus a
small set of **stateless read-only subagents** for context-heavy analysis (repo comprehension
sweeps, failure clustering, test drafting) that return structured results and are discarded.
Rationale: §31.2 already promises "a single coherent agent"; durable state lives in platform
objects, never in agent memory — which is also what lets sessions resume and satisfies §39's
requirement that the platform stay usable when the LLM is unavailable.

**§31B — Session state and context budget.** New `harness_sessions` / `harness_messages` /
`harness_tool_calls` tables (distinct from §40's audit records, joined by correlation ID). Context
layers get explicit budgets, ordered stable → volatile so the cached prefix holds:

| Layer | Content | Budget | Cached |
|---|---|---|---|
| 0 Session | request, selection, recent turns | volatile | after breakpoint |
| 1 Project summary | metadata, framework, agents, workflows, tools | ~4k | ✓ |
| 2 Workflow | entrypoint, graph, tool contracts | ~8k | ✓ |
| 3 Task evidence | search results, excerpts, cases, traces | ~20k, evictable | — |
| 4 Execution | live run state, results, failures | streamed | — |

Cache health is verified via `cache_read_input_tokens`, not assumed. Overflow uses compaction
rather than silent truncation.

**§31C — Mutations are transactional and idempotent.** Multi-object creation commits atomically or
rolls back; every mutating tool call carries a client-generated idempotency key so a model retry
cannot double-create. Drafts are garbage-collected on abandonment.

**§36A — Confirmation as a mechanism, not prose.** Static classification plus argument-sensitive
rules, which is what fixes §38:

```yaml
confirmation:
  static: read_only | mutating | destructive
  dynamic:                                     # CEL over the tool's own arguments
    - { when: "args.case_count > 100",         require: explicit_confirm }
    - { when: "args.estimated_cost_usd > 5",   require: explicit_confirm }
```

Registry subsetting is defined: a base set always present, phase-scoped additions, and deferred
tool loading with tool-search once the registry outgrows reliable selection.

**§32B — Ingestion as an observable job.** Staged, resumable, with progress events and **partial
availability** (browse the file tree while semantic analysis runs). LLM analysis is **scoped and
capped**: files ranked by entrypoint proximity and framework signals, a hard token budget, and the
selection recorded so it is reproducible. A file-level dependency graph makes §32.6's "affected
nodes" well-defined. `.env` and credential-bearing files are excluded from inventory, index, and
embeddings, and archives are secret-scanned before extraction.

**§32C — The Project Model carries confidence, not assertions.** Every discovered entity gets a
confidence score, provenance (file, symbol, line range), and a **user override** that outranks the
parser permanently. Runtime-registered tools — the reference agent's exact pattern — are expected
to score low from static analysis and are confirmed instead from the **first smoke-run trace**,
which observes what actually got called. Static analysis proposes; execution confirms.

**§33A — Retrieval made concrete.** Reciprocal-rank fusion across lexical/structural/semantic;
symbol-level chunking with a file-context header; embedding invalidation keyed to §32.6 content
hashes; per-call result and byte budgets; and workspace scoping enforced **in the query, not in the
prompt**. Reads are audited alongside writes.

**§34A — Discovery that ranks and reports coverage.** Candidates are deduped, grouped, capped, and
ranked by `risk × uncovered × evaluator cheapness` — deterministic checks surface before judges.
`business_importance` and `risk_level` are explicitly labeled **inferred** until a user confirms
them. Discovery's headline output becomes a **coverage report**: which workflow branches, tools,
and error paths have no evaluation — the question §34 currently cannot answer.

**§10B — Conversational test-case authoring** *(the chat flow A17 is missing)*:

```
user intent → LLM drafts cases → schema validation → dedup (exact + embedding)
   → expected values tagged  user_stated | inferred | derived_from_trace
   → preview as a diff against the current dataset → user commits → new dataset version
```

The governing rule: **an LLM-inferred expected value is never silently authoritative.** It is
marked `inferred`, shown in review, and reported as a dataset-health figure — because a wrong
expectation means confidently evaluating the wrong thing, which is §28's own stated risk and the
hardest failure to notice.

**§35B — Queue, streaming, resume.** Priority and per-workspace fairness with backpressure;
results stream as cases complete rather than appearing at an `AGGREGATING` barrier; and a failed
run **resumes only the failed cases** against the frozen manifest.

**§37B — Dashboard definition schema and binding language** *(closes A20)*. The artifact the LLM
emits, defined as concretely as §9A's trace rules:

```yaml
dashboard:
  version: 3
  layout:  { type: grid, columns: 12 }
  filters: [ { id: run, type: run_selector, default: latest } ]
  blocks:
    - component: MetricCard
      span: 3
      bind: { metric: eligibility_guard, run: "$filters.run", stat: pass_rate }
    - component: TraceTimeline
      span: 12
      bind: { case: "$selection.case" }        # cross-block selection
```

Components declare an `inputs` schema; the validator type-checks every binding. Bindings resolve
**server-side against pre-aggregated results**, never raw trace scans (§24). The registry is
versioned, saved dashboards pin a version, and removing a component requires a migration.

**§37C — Plugin trust model** *(closes A21)*. Enumerated permissions, manifest compatibility
enforcement, and an explicit position on UI components: **first-party reviewed components only for
MVP**, with sandboxed-iframe + `postMessage` named as the extension path. Third-party components
never execute unsandboxed in the app shell — otherwise the plugin system silently reintroduces the
arbitrary-frontend risk §37.1 exists to prevent.

---

## Part 4 — Domain design review (four parallel agents)

Four specialist agents reviewed this plan in parallel — product/UX, frameworks/architecture,
infrastructure/Python, and data/performance — cross-communicating through the coordinator. Their
findings are merged below. **They converged rather than conflicted**, and several closed each
other's gaps.

### 4A. Corrections to Parts 1–3

Six places where the review overturned what this plan previously said. These supersede the earlier
text.

| # | What Parts 1–3 said | Why it was wrong | Corrected position |
|---|---|---|---|
| C1 | §32A: "one successful invocation is required **before metric authoring unlocks**" | Serializes human time behind machine time. Authoring is a conversation over the *analysis*, not a running container — and when runtime prep fails (the reference agent's case, argued to be the **modal** first run) the user is locked out of the only thing they came to do. | **Smoke gates RUNNING only.** Drafting proceeds during image build, carrying `awaiting smoke` badges; specs validate at run time. |
| C2 | "Not in scope: v2's §1–§6 product framing, which is good and carries over largely intact" | This exempted §3 personas, §4 journey, and §5 chat/IA from review — precisely the least-hardened sections in the document. | **In scope.** §3A, §4A–§4C, §5A–§5B are new v3 sections. |
| C3 | §11C: `on_mismatch: compare_intersection_and_warn` | Unimplementable — test cases have **no stable identity across dataset versions**, so there is nothing to intersect on. Regression comparison across a dataset bump would be silently wrong. | `test_cases.case_key = hash(normalized input)`, stable across versions, plus per-version `content_hash`. |
| C4 | Decision 5 commentary: LiteLLM Proxy might *become* the egress proxy | **LiteLLM Proxy is an API server, not an HTTP CONNECT forward proxy** — it intercepts nothing. Zero-code-change capture requires forward proxying. It also accepts only OpenAI-format requests, while the reference agent sends native Gemini `generateContent`. | Custom egress proxy with the **`litellm` SDK (`Router`) embedded** as its outbound leg. LiteLLM Proxy server is at most an optional self-host artifact, never in the run path. |
| C5 | §12A: local non-HTTP tool calls are "invisible to the proxy" and this is "a stated boundary" | Accepting it would make tool-argument and tool-output metrics — core use cases — unscoreable on the reference agent, whose three tools are pure local `_run` methods. | **In-sandbox enrichment shim**, two layers: framework-native hooks, falling back to meta-path import hooks that wrap discovered tool symbols. |
| C6 | §12A amendment proposed a `capture_gap` for traffic the proxy cannot see, with the budget unenforceable against it | Infrastructure closed the hole entirely: with no route off the internal network and **DNS answering every name with the proxy IP**, traffic the proxy cannot see *cannot exist*. | Budget is enforceable **by construction**. `capture_gap` survives only as an observability signal, not a correctness hole. |

### 4B. Newly locked decisions

| # | Area | Decision |
|---|---|---|
| L1 | Model abstraction | Custom egress proxy embeds `litellm.Router` for the outbound leg; platform's own calls go through a `ModelGateway` interface. LiteLLM cost figures are **advisory**; the proxy recomputes from raw usage against a versioned price table and is **authoritative**. |
| L2 | Harness runtime | Anthropic SDK `tool_runner` behind `ModelGateway`. **No LangChain/LangGraph/CrewAI in the control plane** — self-referential hazard (evaluating a framework you are built on) plus dependency conflict with users' agents. |
| L3 | Adapters | In-sandbox shim, pre-baked into the agent image at build. Reports via HTTP POST to the proxy's `/_enrich` (no shared volumes with untrusted code). **Compromised-by-default**: never authoritative for cost, usage, or timing; proxy wins every conflict; join key is `provider_request_id`. |
| L4 | Runner | Worker is an **API client to a rootless Podman socket** — never a daemon holder, never Docker-in-Docker. gVisor `runsc` on Linux where available; runc + rootless userns elsewhere. One knob: `AGENT_RUNTIME`. |
| L5 | Network | **One internal network per case**, containing only the agent and the per-run proxy (multi-homed onto every active case network). Run-scoped DNS answers all names with the proxy IP. Proxy terminates TLS via a CA baked into agent images. |
| L6 | Packaging | **uv** workspace + lockfile; Python ≥ 3.12. Platform and agent never share an interpreter. |
| L7 | Queue | **Redis + arq** (async-native, matches FastAPI). Redis also carries leases and SSE pub/sub; durable state stays in Postgres. Job types: `build`, `smoke`, `run_case`, `re_evaluate`, `gc`. |
| L8 | Proxy lifecycle | **Per-run instance**, not pooled — isolated budget meter, isolated trace namespace, crash containment, clean per-run partition key. |
| L9 | Trace storage | RANGE-partition on `run_id`, **one partition per run**. Retention is `DROP PARTITION`, never `DELETE`. No `DEFAULT` partition — unknown `run_id`s are rejected, not silently absorbed. |
| L10 | Payloads | Typed columns for every queryable dimension + one redacted `jsonb` ≤ 32 KB in-row; beyond that `payload_ref` to object storage. **No GIN index on payload** — every hot path is served by typed columns. |
| L11 | Aggregation | Write-time incremental for everything the dashboard touches. `aggregation_state` (`IN_PROGRESS/PARTIAL/COMPLETE`) lives on the metric row, **separate from `runs.status`**, so a streaming half-run renders with a banner and can never display as final. |
| L12 | Vectors | pgvector in the primary database, `embeddings` LIST-partitioned by `workspace_id` with per-partition HNSW; **pgvector ≥ 0.8 required** (pre-0.8 filtered ANN silently under-returns); `halfvec(1024)`. |
| L13 | Wedge persona | **The agent developer** — the only persona holding code, agent, provider key, and urgency. Every v1 feature is justified against that loop. |

### 4C. Product & experience

**§4A — First-run time budget.** First useful content ≤ 10 s; project understanding ≤ 60 s;
runnable ~5 min cold / under a minute warm; first scored run ≤ 15 min. Governing rule: *no human
time is ever spent waiting on machine time that could have run earlier or in parallel.* No state
may be a bare spinner for > 10 s without a "what's happening" line.

**The ghost run.** The smoke invocation is not a checkbox — it is the first deliverable. Its trace
is persisted and rendered as the product's proof moment ("here is your agent's first real
execution"), and its probe cases seed dataset version 1.

**§4B — Onboarding recovery: `DECLARE → PREPARE → SMOKE`.** The gate splits into three named
stages, each with first-class recovery UI. Six classified failure states — `entrypoint_missing`,
`install_failed`, `invocation_failed`, `provider_unreachable`, `no_trace`, `timeout` — each with
distinct copy and recovery actions, an attempt log, and a **"reproduce locally" command** so
debugging happens in the user's own environment. Per-framework entrypoint declaration forms, plus
a shim generator that shows the file for review before use.

**§5A — Conversation state machine + message taxonomy.** States `IDLE → CONTEXT_GATHERING →
PROPOSING → AWAITING_DECISION → EXECUTING → REPORTING`, plus `DEGRADED` (harness LLM unavailable,
deterministic operations still usable) and `RESUMED`. Ten typed message kinds (`spec_proposal`,
`spec_diff`, `run_card`, `approval_card`, `error_card`, …) so the frontend never renders state as
prose. **Ownership rule: chat references persisted objects; the panel owns state** — a proposal is
a stored draft spec, so a refresh re-renders the exact decision point.

**§5B — Async UX.** A pinned **run card** that survives refresh; **planned continuations** ("when
it finishes, explain the failures" — persisted and executed on completion); a seven-type
notification taxonomy with `regression_detected` as the primary bring-back.

**§13A.1 — Dispute and re-score.** Inspect → dispute → annotate → re-score. Three dispute paths
(rule wrong / evidence missing / judgment wrong), with **overrides never destructively replacing a
machine score** — both are stored and rendered side by side. The headline capability:
**re-score from stored traces with no agent re-run**, turning metric iteration from hours into
seconds. Judge disputes feed the calibration set directly.

**§15A.1 — Judge readiness, not a gate.** Parts 1–3 made κ a hard gate, which is a day-one
chicken-and-egg: nobody builds a calibration set before seeing the judge work once. Replaced with
three states — `UNCALIBRATED` (usable, every score badged provisional), `CALIBRATING`,
`CALIBRATED` — and a guided "label these 10 cases" flow.

**§10B.1 — Starter metrics.** Six framework-agnostic deterministic metrics that apply to any traced
agent (terminates within budget; no tool called more than N times; no secret/PII in tool args;
non-empty typed final response; no unrecovered tool-error crash; no `budget_exceeded`), shipped as
an "Agent Health" template activated in one click. The first 30 minutes produce a scored run
without the user typing a sentence of domain intent.

### 4D. Frameworks & architecture

**§12C — Model gateway.** One rule: *the egress proxy is authoritative for what happened on the
wire; LiteLLM is authoritative for how to talk to a provider.* Callback-to-event mapping table for
`llm_call` / `llm_response` / `error` / `retry` / `stream_start` / `first_token`.

**Judge comparability across providers** — a problem multi-provider support creates that did not
exist before. Determinism comes from **schema, not sampling**. `judge_binding = (provider, exact
model id + version, schema version, rubric version)` is recorded in the manifest; runs compare only
when bindings match; **judge calls never use fallbacks** (a mid-run fallback changes the
instrument — the run is flagged `mixed_binding` and excluded from regression comparison). A binding
change triggers κ recomputation and a score-distribution parity check before use.

**§37A — Revised adapter contract.** `discover / prepare / build_shim / smoke / invoke / collect /
supports`, returning `DiscoveryReport` (confidence + provenance + `needs_entrypoint_declaration`),
`ShimBundle` (hooks, symbols to wrap, side channel, **version ladder** for older framework APIs),
and `EnrichmentReport` (always `untrusted: true`).

**§32C — Confidence as numbers, not prose.** Framework import 0.9; entrypoint call 0.8; framework
constructed but entrypoint absent **0.2** (the reference agent); `BaseTool` subclass 0.7; **tool
registered in a runtime dict 0.3**; any tool after smoke-trace observation **0.95**. Static
inventory **never exceeds 0.6** without execution confirmation.

**Instrumentation matrix** for CrewAI (typed event bus, with per-agent listener fallback),
LangGraph/LangChain (callback injection into `Runnable` config — the zero-code-change point),
OpenAI Agents SDK (`RunHooks` + span exporter; documented gap: tool hooks don't fire for hosted
tools or `Agent.as_tool()`), and plain Python (meta-path import hook).

**On the reference agent specifically:** the proxy sees the manager's **delegation decisions**
(CrewAI's `allow_delegation=True` injects a `delegate_work` tool, so the choice appears in the
Gemini request body) but **zero tool executions**. A "did it delegate correctly" metric is
proxy-only scoreable; "what did the sub-agent actually do" requires the adapter.

**LiteLLM's sharp edges — verified, and each one a silent failure if unhandled.** These are stated
in v3 so they are never re-litigated at build time:

- **It silently drops `temperature` for Anthropic** while OpenAI honours it. Same call, two
  providers, different semantics, no error. Judge comparability via parameter parity is therefore
  impossible and must not be attempted — hence schema + calibration gating.
- **Its Anthropic usage passthrough has dropped `cache_read_input_tokens`** — the exact field
  §31B needs to verify prompt-cache health. This is a concrete reason the harness uses the
  Anthropic-direct gateway rather than routing everything through LiteLLM.
- **Its batch passthrough is OpenAI-centric.** §15A's "judging at 50% cost" must call
  **provider-native batch endpoints directly**, or it silently degrades to full-price calls.
- **Its price tables drift for non-OpenAI providers** — which is why the proxy recomputes cost from
  raw usage against its own versioned table and treats LiteLLM's figures as advisory.

**`pause_turn` is a silent truncation hazard.** The Python SDK tool runner does not auto-resume
paused turns; a naive loop returns a truncated final message as success. The harness must mirror
history, restart with the paused turn appended, cap restarts, and emit an explicit truncation
marker when the cap is hit.

Four Mermaid diagrams accompany v3: platform components, one test case end-to-end, one harness
conversation turn, and the proxy/adapter merge flow.

### 4E. Infrastructure & runtime

**Three-layer egress enforcement** (this is what closes C6): (a) internal network with no default
route — hardcoded-IP calls fail closed; (b) run-scoped DNS answering **every** query with the proxy
IP, so no DNS-tunnel exfil channel exists by construction; (c) TLS termination at the proxy with
SNI routing to allowlisted provider dialects. A **network-verification probe** runs as a smoke step
and asserts all three.

**§11A — Invocation protocol, finished.** `/input/case.json`, `/input/manifest.json`,
`/output/result.json` (atomic write). **Expected values are never shipped into the sandbox** — the
agent would game them. Exit `0` = completed; **runner-applied kills are never agent exit codes** —
`TIMED_OUT` / `BUDGET_EXCEEDED` / `CANCELLED` / `ORPHANED` are lifecycle statuses decided by the
worker. Metric results are computed **only for `COMPLETED` attempts**; a crash is never scored as
an assertion failure. SIGTERM → 10 s grace → SIGKILL; stdout/stderr capped at 1 MiB each.

**Image build.** Digest-pinned base, warm-pooled at deploy so users never pay the base pull. Cache
key `sha256(base_digest | manifest_hash | adapter_version | runtime | platform)`. **Failed builds
write zero cache entries** — one workspace's broken install cannot poison the shared cache.
Build-time egress is a single allowlisted caching package index, never open. 10-minute hard
timeout, SBOM + vulnerability scan + signing. **No-manifest case** (the reference agent's): build
succeeds with base+code, and the smoke run surfaces the ImportError with "add a dependency
manifest" as the recovery action.

**Orphan reaping.** Redis leases (120 s TTL, refreshed every 20 s) plus a **separate `sweeper`
process** — a worker crash must not take the reaper with it. Proxy flushes its event buffer on
SIGTERM so an orphaned case's events land before the sweeper marks it.

**Archive defenses.** Header scan *and* enforcement during extraction (bomb headers lie): 2 GiB
total, 512 MiB per entry, ratio ≤ 100, ≤ 10,000 entries. All symlinks rejected by default. Secret
scan quarantines `.env`, `*.pem`, `id_rsa*`, `credentials*` **before** anything reaches inventory,
index, or embeddings.

**Repo layout** — `apps/{api,web}`, `packages/{evaluation-schema,trace-schema,runner-protocol,
archive-safety,evaluator-sdk,dashboard-schema,proxy-core}`, `services/{worker,sweeper,builder,
proxy,project-understanding}`, `infra/{docker,package-proxy,compose}`, `migrations`, `tests/
{unit,integration,sandbox}`. Nine Compose services across `front` / `control` / `build_net`, plus
dynamic per-case networks.

**macOS caveats — these affect your machine directly.** gVisor is **Linux-only**, so local dev
isolation is VM-grade, not gVisor-grade. **Docker Desktop is rootful-only and cannot host this
design** — Podman Machine is required. The compose client and the in-VM services use **two
different socket paths**, so `ENGINE_SOCKET` must come from env. First `podman machine init` pulls
a VM image (~10 min, one-time).

### 4F. Data & performance

Full DDL for the hot path — `trace_events`, `cost_ledger`, `run_cases`, `run_case_attempts`,
`run_case_metric_results`, `run_metric_results`, `runs`, `scoring_jobs`, `failure_clusters`,
`annotations`, `notifications`, `embeddings`, `cost_summaries` — with per-partition indexes.

**Nine query patterns**, each with access path, index, and cost: dashboard load < 5 ms with zero
trace reads; failed-case drill-down 10–50 ms index-only; trace timeline a point read;
**evidence resolution O(evidence), never O(run)** — which is what actually closes Part 1's
"0.5 GB per run before evidence linking" landmine; run comparison < 100 ms.

**Scoring pipeline.** Stage 1 (deterministic) scores each attempt as its merge window closes;
stage 2 (judges, run-scoped metrics, clusters) runs as `scoring_jobs` under a `score_revision`.
Re-score is a new revision — idempotent, diffable, last 5 retained.

**Sizing, reconciled with frameworks.** ~30–60 events per case attempt on the reference agent
(typical 60–200; delegation-heavy up to ~400). A max run (10,000 cases × 5 repeats) is **7.5–10M
events, 30–50 GB raw**; a 500-case nightly run is 1–2.5 GB. Peak ingest ~5k events/sec.

**Additional gaps closed:** per-attempt cap of **1,000 events** with a `trace_truncated` marker
(loop safety needs a storage bound); separate `repeat_index` and `attempt` columns with
first-attempt-authoritative flagging, so "passed on retry" cannot re-enter through the back door;
`price_version` on every cost row (provider pricing drifts, so historical totals and cost gates
silently rot without it); **`case_evidence_snapshots` written at run-complete** — roughly 1000×
compaction (~10 KB/attempt vs 4–40 MB raw), so scores never outlive their evidence, with a
first-class "payloads expired" state as the further fallback; a `maint_role` with `BYPASSRLS` for
retention, never held by app connections.

Three that would have been "fixed" into bugs later:

- **`trace_events` deliberately has no foreign key to `runs`.** Partitioned-FK enforcement costs
  per-insert on the hottest table; validity is enforced by the ingest token plus a run-state check.
  This must be stated in v3, or someone will helpfully add the FK and every ingest pays for it.
- **Read auditing must be sampled and async.** §19's "audit reads, not just writes" applied
  literally to the dashboard path would multiply write volume on read-mostly hot tables.
- **`harness_messages` carry user PII.** Redaction-at-write applies to conversation state too, not
  just agent-under-test traces — §32B's `.env` embargo has no equivalent for chat until this is
  stated.

**Adapter events are first-class evidence.** Tool-argument and tool-output metrics are scored
*entirely* from `source='adapter'` events, so §9A's trace-rule operators must not filter by source
unless a metric explicitly asks. Adapter evidence is not optional decoration.

### 4G. Cross-domain resolutions

| Tension | Resolution |
|---|---|
| Product's time-to-value vs infra's cold build | Negotiated: 2–6 min cold / 30–60 s warm accepted, **in exchange for authoring being decoupled from smoke** (C1). 10-min hard cap for pathological repos. |
| Frameworks' shim side channel vs infra's threat model | Infra overrode the socket/file proposal: **HTTP POST to the proxy's `/_enrich`** — no shared volumes with untrusted code, proxy re-runs redaction and rate-limits. |
| Frameworks' `capture_gap` vs budget enforcement | Infra's DNS hijack removes the gap entirely (C6). |
| Database's five infra assumptions | All five confirmed, with one correction: **the API executes the COPY, not workers** — workers never write traces, and redaction happens before events leave the proxy. |
| Adapter event volume | Frameworks revised 2× → **3–5×**; database's 500-events-per-attempt cap absorbs it. Reconciled to 3–5× for partition sizing. |
| Judge κ as a hard gate | Product overrode: `UNCALIBRATED` is usable-with-badge (§15A.1). |

### 4H. Scope cuts

Justified against the wedge (L13). Each is deferred with a mechanism-level placeholder, not deleted:
production replay as a v1 path (cassette mechanism stays); pairwise A/B as a UI (judge capability
stays); error-path coverage inference; evaluator SDK and marketplace; dedicated LangGraph and
OpenAI Agents adapters (they still run via generic + proxy, just without in-process badges); CI/CD
pipeline configuration UI (API and webhooks stay); human review queues as a workflow;
dashboard plugin extension mechanism; embeddings-based failure clustering (rule-based for MVP).

**Two requirements explicitly override every cut:** streaming partial results, and
re-score-without-rerun. Both are confirmed feasible by database and infrastructure.

### 4I. Run sizing and tiers

Negotiated across product, infra, and database: throughput ~10 cases/min sustained, so 500 cases
≈ 50 minutes and 5,000 ≈ 8 hours. **Hard cap 5,000 cases per run**, with a warning at 1,000.
Runs are offered in tiers at the confirmation card — **Quick (≤20, ~2 min)** as the standard first
run, **Standard (100–200)**, **Full (up to 5,000)** — each with its own duration and cost estimate.
This is also what corrects §38's "Run 500 cases": it now shows "≈50 min estimated" before approval.

### 4J. Decisions taken

Everything the four agents raised is closed. The four items that were genuinely the owner's call
have been decided:

| Decision | Outcome |
|---|---|
| **Scope cuts (4H)** | **All ten accepted.** Every one is mechanism-recoverable — cassettes stay so replay is buildable later; the judge's pairwise capability stays so the A/B UI is buildable later. |
| **Judge gating** | **Usable but provisional.** `UNCALIBRATED` judges run from day one, every derived score badged provisional and excluded from gate enforcement until calibrated; a guided "label 10 examples" flow, fed by disputed scores, bootstraps the calibration set. Rejected: hard κ gating with mandatory first-run calibration. |
| **Example fixture** | **Repaired copy of the reference agent**, versioned, with the repair disclosed — missing tail, a `kickoff` entrypoint, and the corrected model string. It stays a real CrewAI/Gemini multi-agent system, and still exercises the no-entrypoint and runtime-tool-registry paths. |
| **Isolation / deployment** | Superseded by decision 6 — isolation is a **tier** (§4K), not a deployment choice. Tier 1 runs everywhere; Tier 2 (`runsc`) is opt-in on Linux for anyone running this as a service. |

**Still open: the license.** It is a real choice for a platform others may run commercially, and it
is the one decision here with no reversible default.

### 4K. Open source and platform support

Decision 6 arrived after the four-agent review and revises its infrastructure conclusions. The
agents designed for a single-tenant Linux/macOS deployment; an open-source project has different
constraints — **many self-hosters, varied machines, and a public repository.**

**Isolation becomes a tier, selected by one `AGENT_RUNTIME` knob.** The trust model varies by
deployer, so the architecture must not.

| Tier | Runtime | Platforms | Appropriate when |
|---|---|---|---|
| **1 — default** | rootless Podman + runc, seccomp, no egress except the proxy | Linux, macOS, Windows/WSL2 | Self-hosters evaluating **their own** agents. Proportionate, and the only tier that runs everywhere. |
| **2 — opt-in** | adds gVisor `runsc` | Linux only | Hardened or shared deployments; anyone running the project **as a service** for others. |
| **3 — future** | per-run microVM (Firecracker/Kata) | Linux | Public multi-tenant with untrusted uploads. Named as the path; not built. |

**Tier 1 is not a security fiction.** It still enforces every boundary that does not depend on
kernel virtualization: no route off the internal network, DNS answering only the proxy, read-only
rootfs with tmpfs scratch, CPU/memory/PID/wall-clock caps, no Docker socket anywhere, and
credentials that never enter the container. gVisor hardens the kernel boundary specifically; it is
not what makes the sandbox a sandbox.

**Platform matrix.** Every platform runs *Linux containers* — Windows containers are never used.

| Platform | Engine | Notes |
|---|---|---|
| **Linux** | native rootless Podman | Reference platform. The only one where `runsc` is available. Full CI. |
| **macOS** | Podman Machine (Linux VM) | **Docker Desktop cannot host this design — it is rootful-only.** Compose client and in-VM services use *different* socket paths, so `ENGINE_SOCKET` comes from env. Keep run mounts on in-VM tmpfs; host bind mounts cross gRPC-FUSE and are slow. |
| **Windows** | Podman or Docker Desktop on **WSL2** | The stack runs inside the WSL2 Linux VM; Windows is the client only. Path translation at the boundary, `core.autocrlf` guidance for contributors, and the case-insensitive filesystem makes archive extraction collision checks load-bearing rather than theoretical. |

**Open-source consequences that change the build:**

- **The proxy CA is generated per install, on first run, and never committed.** A shipped MITM CA
  private key would let anyone decrypt traffic from every deployment. The repo carries the
  generation step, never the key. This is the single most dangerous thing to get wrong here.
- **No telemetry, no phone-home, no bundled credentials.** Users bring their own provider keys.
- **`docker compose up` must be the whole quickstart on all three platforms** — that is the real
  adoption bar for a self-hosted project, and it is a testable claim.
- **CI is a three-platform matrix**: Linux (full, plus a nightly `runsc` job), macOS, and Windows.
  A contributor on any of the three must be able to run the test suite.
- **Adapters are the natural contribution surface.** The `AgentAdapter` protocol (§37A) is what
  lets someone add LangGraph or OpenAI Agents support without touching the core — which also makes
  the framework-adapter cut in §4H safe, since the community can fill it.
- **The design documents ship in the repository.** This review and v3 are the project's rationale
  record.
- **License is undecided** and is a real choice for a platform others may run commercially.

## Verification

The deliverable is a document, so verification is structured review, not tests:

1. **Gap closure** — walk G1–G12, A1–A21, and all eight contradictions in 1B; each must map to a
   named v3 section. No "TBD" anywhere.
2. **Use-case coverage** — U1–U14 each appear in the §3 table *and* carry a stated evaluator
   strategy, so none is aspirational.
3. **Reference-agent walkthrough (engine)** — trace `multi_agent_system.py` through v3 end to end:
   ZIP ingest → discovery finds CrewAI but **no entrypoint** → user declares one (§32A) → smoke
   gate → proxy captures the Gemini calls (§12A) → a delegation metric (U6) and a tool-argument
   metric score against the trace → evidence links resolve → cost is attributed.
4. **Reference-agent walkthrough (agent layer)** — the same file through §31–§37: the three
   **runtime-registered tools** score low confidence from static analysis (A8) and are confirmed
   from the smoke trace (§32C) → discovery ranks candidates and reports which branches are
   uncovered (§34A) → the user adds a case in chat and its inferred expectations are marked, not
   silently trusted (§10B) → a 500-case run trips the argument-sensitive confirmation rule (§36A)
   → a dashboard definition validates against the component registry (§37B). Any step without a
   defined answer means v3 is not done.
5. **Buildability** — a reader must be able to implement the proxy, the trace-rule evaluator, the
   metric semantics, the invocation protocol, **and the dashboard binding language** from v3
   alone, without inventing design.
6. **Self-consistency pass** — no section contradicts another; this is the exact failure mode
   that produced 1B, so v3 gets an explicit pass for it.

## Not in scope

Implementation sequencing and phase planning; any code; and a rewrite of v2's §1–§6 product
framing, which is good and carries over largely intact.
