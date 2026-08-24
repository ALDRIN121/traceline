**LLM-Based Agent  
Evaluation Engine**

Product, UX, Evaluation Model, and Technical Architecture

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>Core thesis</strong></p>
<p>The platform is not primarily an LLM-as-a-judge product. It is a configurable evaluation engine in which an LLM helps users understand an agent and translate natural-language evaluation intent into executable, composable metrics and evaluators.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

Status: Product / technical design specification

Target release: MVP first, extensible architecture for production scale

# 1. Executive Summary

The proposed platform is a conversational evaluation operating system for agentic applications. A developer connects an agentic codebase or workflow, the platform inspects the project, identifies the workflows and observable execution points, and helps the developer define exactly what should be true about the system. Those evaluation definitions are converted into structured specifications, executed against the real agent in an isolated runtime, scored using configurable evaluators, and presented through explainable dashboards and trace views.

The core differentiation is the evaluation abstraction. The system does not assume that every agent should be judged using one generic LLM score. Instead, a user can create metrics that inspect tool inputs, tool outputs, final answers, state transitions, workflow sequences, latency, structured values, expected references, or any custom condition. LLM-based judging is supported where semantic judgment is genuinely required, but it is one evaluator type among many.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>Design principle</strong></p>
<p>The LLM is the intelligent interface and orchestration layer. The evaluation engine is the source of truth for execution, validation, scoring, persistence, and reproducibility.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## Primary outcome

A team should be able to bring a real agent into the platform and go from “I want to know whether this works correctly” to a repeatable evaluation suite with executable tests, transparent metrics, trace-level evidence, and version-to-version regression detection without building a custom evaluation framework from scratch.

## Product loop

> Connect project → Understand code → Discover workflow → Define evaluation → Generate tests → Preview → Execute → Evaluate → Explain → Compare → Iterate

# 2. Problem, Vision, and Product Principles

## Problem statement

Agentic systems are difficult to evaluate because their behavior is not always captured by a final text response. A single task may involve multiple model calls, tool selections, tool arguments, external results, intermediate state, retries, policy checks, and a final response. Different teams care about different notions of correctness, and many useful checks are deterministic or domain-specific rather than naturally suited to an LLM judge.

## Vision

Build an evaluation engine where “what good looks like” is configurable at the level of the agent execution itself. A user should be able to define assertions against any observable part of a workflow and combine those assertions into a repeatable evaluation suite.

## Product principles

- Configurable over prescriptive: do not force teams into a fixed metric taxonomy.

- Observable over opaque: every score should be traceable to evidence.

- Deterministic where possible: use code and rules whenever the requirement can be expressed exactly.

- LLM-assisted, not LLM-dependent: use an LLM to make evaluation design easier, not to replace the evaluation engine.

- Reproducible: every run identifies the agent, dataset, evaluator, metric, and model versions involved.

- Composable: simple metrics can be combined into higher-level evaluations.

- Safe by default: user code executes in isolated environments with explicit resource and network boundaries.

- Progressive disclosure: the first-time user can stay in chat; expert users can inspect and edit structured evaluation specifications.

# 3. Users and Use Cases

## Primary users

| **Persona**                | **Need**                                                    | **Primary workflow**                                 |
|----------------------------|-------------------------------------------------------------|------------------------------------------------------|
| Agent developer            | Know whether a new agent version still behaves correctly.   | Connect code → define metrics → run regression suite |
| Applied AI / ML engineer   | Build repeatable evaluation suites and compare experiments. | Dataset → metrics → runs → analysis                  |
| QA / reliability engineer  | Validate workflow invariants and edge cases.                | Rules → traces → failures                            |
| Product / operations owner | Understand whether the agent is meeting business outcomes.  | Dashboard → trends → failure clusters                |
| Platform engineer          | Standardize evaluation infrastructure across teams.         | Projects → reusable evaluators → policy              |

## Core use cases

| **Use case**                 | **What the platform must enable**                                                                              |
|------------------------------|----------------------------------------------------------------------------------------------------------------|
| Tool output correctness      | For a given query and expected state, assert that a specific tool returns the correct value or structure.      |
| Tool argument correctness    | Verify that the agent passes the right identifier, amount, filter, date, or other arguments to a tool.         |
| Tool selection               | Verify that the correct tool is called for a particular request, or that forbidden tools are never called.     |
| Workflow compliance          | Verify that required steps happen in the correct order, such as eligibility check → approval → action.         |
| Final response correctness   | Compare the final answer to an expected output, schema, reference answer, or semantic rubric.                  |
| Business rule validation     | Encode domain rules such as refund limits, approval policies, pricing constraints, or escalation criteria.     |
| Structured output validation | Require JSON, fields, types, ranges, enums, or schemas to be valid.                                            |
| Latency and cost evaluation  | Track execution time, token usage, tool latency, and cost against thresholds.                                  |
| Adversarial evaluation       | Generate or maintain tests designed around edge cases, policy boundaries, malformed inputs, and failure modes. |
| Regression testing           | Run the same evaluation suite against multiple agent versions and identify material regressions.               |
| Failure analysis             | Cluster failures, inspect traces, and explain likely root causes using evidence.                               |
| Production replay            | Replay selected or anonymized real interactions against candidate versions for pre-release validation.         |

# 4. End-to-End User Journey

| **Step**                     | **Experience**                                                                                                                          |
|------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| 1\. Create project           | Upload a ZIP, connect a repository, or provide a mounted/local project path.                                                            |
| 2\. Inspect project          | The platform performs static analysis and targeted LLM analysis to identify agents, workflows, tools, models, prompts, and entrypoints. |
| 3\. Select workflow          | The system presents detected workflows as evaluation targets.                                                                           |
| 4\. Open evaluation chat     | The user enters a conversation with the evaluation harness, not merely the underlying agent.                                            |
| 5\. Define objective         | The user explains what they want to know in natural language.                                                                           |
| 6\. Generate evaluation plan | The LLM proposes metrics, targets, test categories, expected values, and scoring rules.                                                 |
| 7\. Review and edit          | The user can accept, reject, or manually adjust the structured definition.                                                              |
| 8\. Generate test suite      | The system creates or imports test cases and validates the dataset.                                                                     |
| 9\. Preview                  | A mock-data HTML/dashboard preview shows the user how the evaluation will work.                                                         |
| 10\. Execute                 | The runner executes the agent in an isolated container and records traces.                                                              |
| 11\. Evaluate                | Configurable evaluators compute metric-level results.                                                                                   |
| 12\. Analyze                 | The user sees aggregates, failed cases, clusters, and trace evidence.                                                                   |
| 13\. Compare                 | The run can be compared with prior runs or agent versions.                                                                              |
| 14\. Iterate                 | The user edits metrics/tests and reruns the evaluation.                                                                                 |

# 5. User Experience and Information Architecture

## Primary navigation

> Workspace  
> └── Project  
> ├── Overview  
> ├── Workflows  
> ├── Evaluations  
> ├── Datasets  
> ├── Runs  
> ├── Traces  
> └── Dashboards

## Project overview

The project overview should summarize the code connection, detected agent/workflow inventory, latest evaluation state, recent runs, and open regressions. It should remain lightweight; detailed evaluation work happens inside a workflow.

## Workflow workspace

> ┌────────────────────────────────────────────────────────────┐  
> │ Workflow: Refund Processing Status: Healthy │  
> ├────────────────────────────────────────────────────────────┤  
> │ Overview │ Evaluations │ Runs │ Dashboard │ Code/Trace │  
> ├────────────────────────────────────────────────────────────┤  
> │ │  
> │ Chat with Evaluation Harness │  
> │ │  
> │ User: I want to verify that refunds never happen before │  
> │ eligibility is checked. │  
> │ │  
> │ AI: I can model that as a trace-based metric... │  
> │ │  
> │ \[Evaluation definition preview\] │  
> └────────────────────────────────────────────────────────────┘

## Chat as control plane

The chat should be able to inspect project context, create and modify evaluation objects, start runs, retrieve results, explain failures, and modify dashboards. It should not silently change production artifacts. Mutating actions should produce a structured preview or explicit action confirmation depending on risk.

## Evaluation builder

The builder should expose both conversational and structured modes. Novice users describe intent in chat; advanced users can open the underlying metric/evaluator definition and edit it directly.

# 6. Core Domain Model

## Project

A persistent container representing a user codebase and its evaluation assets. It owns agents, workflows, datasets, evaluations, runs, and dashboards.

## Workflow

A discovered executable target inside the project. A workflow includes an entrypoint, relevant code references, tool definitions, and an execution graph.

## Evaluation

A versioned collection of metrics, evaluators, test datasets, thresholds, execution parameters, and reporting preferences targeted at one or more workflows.

## Metric

A named assertion or measurement against an observable target. A metric defines what to inspect, what evaluator to use, what result to emit, and optionally a threshold or weight.

## Evaluator

The mechanism that determines whether a metric passes or what score it receives. Examples: exact match, schema, rule, code, trace, reference, numeric, or LLM judge.

## Test case

A structured scenario containing inputs plus optional expected outputs, expected tool calls, expected state, constraints, and metadata.

## Evaluation run

An immutable execution record tying an agent version, evaluation version, dataset version, evaluator versions, model versions, and runtime configuration to a set of test results.

## Trace

A chronological collection of execution events for one test, including model calls, tool calls, tool results, state transitions, and final output as available from the integration.

## Dashboard

A versioned view definition over evaluation results. It describes components, data queries, layouts, and interactions without embedding business logic in the UI.

| **Object** | **Key fields**                                                                     |
|------------|------------------------------------------------------------------------------------|
| Project    | id, name, source, connection, environment, created_at                              |
| Workflow   | id, project_id, entrypoint, graph, detected_tools, version                         |
| Evaluation | id, workflow_id, name, current_version, status                                     |
| Metric     | id, evaluation_version_id, name, target, evaluator_ref, scoring, threshold, weight |
| Evaluator  | id, type, implementation_ref, version, sandbox_policy                              |
| TestCase   | id, dataset_id, input, expected, metadata, version                                 |
| Run        | id, evaluation_version_id, agent_version, dataset_version, status, timestamps      |
| Trace      | id, run_case_id, events, metadata                                                  |
| Dashboard  | id, project_id, definition_version, layout, query_bindings                         |

# 7. Metric and Evaluator Architecture

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>Central abstraction</strong></p>
<p>A metric answers: “What should be true, or what should be measured, about this execution?” An evaluator answers: “How do we determine that?”</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## Metric anatomy

> Metric  
> ├── identity: name, description, owner  
> ├── target: event / step / test / trace / run  
> ├── inputs: actual, expected, context  
> ├── evaluator: evaluator_type + configuration  
> ├── scoring: binary / numeric / categorical / distribution  
> ├── threshold: optional pass condition  
> ├── weight: optional composite contribution  
> └── version metadata

## Evaluator types

| **Evaluator**   | **Purpose**                               | **Example**                            |
|-----------------|-------------------------------------------|----------------------------------------|
| Exact Match     | Compare normalized values                 | actual.status == expected.status       |
| Schema          | Validate structured output                | JSON conforms to required schema       |
| Rule            | Evaluate logical conditions               | amount \<= max_refund                  |
| Regex / Pattern | Validate strings                          | response contains required reference   |
| Numeric         | Compare numeric values                    | latency \< 2000 ms                     |
| Reference       | Compare against expected/reference object | answer fields match reference          |
| Trace           | Inspect event sequences                   | eligibility_check occurs before refund |
| Custom Code     | Run user-defined evaluator logic          | domain-specific Python function        |
| LLM Judge       | Semantic/rubric-based evaluation          | response correctly resolves issue      |
| Composite       | Combine child metrics                     | weighted score across sub-metrics      |

## Metric target types

- Input

- Model output

- Tool invocation

- Tool arguments

- Tool output

- State transition

- Workflow node

- Final response

- Entire test trace

- Evaluation run

- External artifact or reference

# 8. Evaluation Examples

## Example A — Tool output correctness

Requirement: “For a given order query, the order status tool must return the correct status.”

> metric:  
> name: Order Status Accuracy  
> target:  
> type: tool_output  
> tool: get_order_status  
> evaluator:  
> type: exact_match  
> actual: output.status  
> expected: test.expected.status  
> scoring:  
> type: binary

## Example B — Tool argument correctness

Requirement: “The refund tool must receive the order ID supplied or resolved from the user request.”

> metric:  
> name: Refund Order ID Accuracy  
> target:  
> type: tool_arguments  
> tool: refund_order  
> evaluator:  
> type: rule  
> expression: args.order_id == expected.order_id

## Example C — Workflow invariant

Requirement: “Never call refund_order before checking eligibility.”

> metric:  
> name: Refund Eligibility Guard  
> target:  
> type: trace  
> evaluator:  
> type: trace_rule  
> condition: \|  
> every(refund_order) has prior(check_refund_eligibility)  
> and prior.check_refund_eligibility.output.eligible == true

## Example D — Structured output

Requirement: “The agent must return a JSON object containing status, order_id, and ETA with the correct types.”

> metric:  
> name: Response Schema Validity  
> target:  
> type: final_response  
> evaluator:  
> type: json_schema  
> schema_ref: order_status_response_v2

## Example E — Semantic evaluation

Requirement: “The answer should clearly resolve the customer’s issue and not claim actions that did not occur.”

> metric:  
> name: Resolution Quality  
> target:  
> type: final_response  
> evaluator:  
> type: llm_judge  
> rubric_ref: support_resolution_v1

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>Important</strong></p>
<p>The LLM judge is optional. The first four examples can be evaluated entirely without an LLM judge.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

# 9. Evaluation Specification and DSL

The LLM should not directly emit arbitrary code into the execution environment. It should generate a structured, validated evaluation specification. A human or policy layer can inspect it before execution.

## Illustrative specification

> evaluation:  
> name: Refund Safety v3  
> target:  
> workflow_id: refund_workflow  
> dataset:  
> id: refund_tests_v8  
> metrics:  
> - name: Eligibility Check Required  
> target:  
> type: trace  
> evaluator:  
> type: trace_rule  
> condition: eligibility_check_before_refund  
> threshold: 1.0  
>   
> - name: Refund Amount Accuracy  
> target:  
> type: tool_output  
> tool: refund_order  
> evaluator:  
> type: numeric  
> actual: output.amount  
> expected: test.expected.refund_amount  
> tolerance: 0  
>   
> - name: Customer Explanation  
> target:  
> type: final_response  
> evaluator:  
> type: llm_judge  
> rubric_ref: refund_explanation_v2

## Validation rules

- All referenced workflows, tools, datasets, schema IDs, evaluator types, and rubric IDs must exist.

- Metric target and evaluator inputs must type-check.

- Custom code must declare an explicit input/output contract.

- No evaluation can access secrets or production resources unless the execution policy explicitly allows it.

- Scores must use declared units and ranges.

- Every versioned evaluation must be immutable once a run starts.

# 10. Dataset and Test Case System

## Test case structure

> test_case:  
> id: tc_001  
> input:  
> query: "Where is order 123?"  
> expected:  
> tool_calls:  
> - name: get_order_status  
> arguments:  
> order_id: "123"  
> tool_output:  
> status: shipped  
> eta: "2026-08-26"  
> final_response:  
> schema_ref: order_status_response_v2  
> metadata:  
> category: order_status  
> difficulty: medium

## Dataset sources

| **Source**             | **Behavior**                                                          |
|------------------------|-----------------------------------------------------------------------|
| Manual                 | User creates or edits tests in the UI.                                |
| Upload                 | CSV/JSON/JSONL import.                                                |
| Generated              | LLM generates tests from workflow behavior and evaluation objectives. |
| Derived from traces    | Create tests from prior observed executions.                          |
| Production replay      | Import sanitized real interactions for candidate evaluation.          |
| Mutation / adversarial | Generate edge cases from existing test cases.                         |

## Test quality controls

- Schema validation

- Duplicate detection

- Expected-value consistency checks

- Coverage by workflow branch or category

- Difficulty tagging

- Human review status

- Generator provenance

- Dataset versioning

# 11. Execution Architecture

## Execution model

> Evaluation Run  
> ↓  
> Run Orchestrator  
> ↓  
> Container / Sandbox Allocation  
> ↓  
> Agent Entrypoint + Dataset Case  
> ↓  
> Instrumentation / Trace Collector  
> ↓  
> Agent Execution  
> ↓  
> Trace Finalization  
> ↓  
> Metric Evaluation  
> ↓  
> Result Aggregation  
> ↓  
> Persistent Run Record

## Docker runner

For the initial version, Docker should provide isolation for user code, dependencies, filesystem state, and resource limits. The execution contract should expose only the files, environment variables, services, and network access explicitly permitted by the evaluation configuration.

## Execution policies

| **Policy**   | **Controls**                                                      |
|--------------|-------------------------------------------------------------------|
| Runtime      | Maximum execution time, retries, concurrency.                     |
| CPU / memory | Per-run resource ceilings.                                        |
| Filesystem   | Read-only base image plus temporary working directory.            |
| Network      | Disabled by default; explicit allow-list for permitted endpoints. |
| Secrets      | Never mounted unless explicitly required and authorized.          |
| Concurrency  | Limit parallel containers per project/workspace.                  |
| Artifacts    | Cap output size and retention.                                    |

## Worker model

The API layer should submit work to a queue. Workers pick up evaluation jobs, allocate isolated runtimes, emit progress events, and persist results. The MVP may use a relational queue or Redis-backed worker system; the execution API should hide this implementation detail from the product layer.

# 12. Tracing and Observability

Every evaluation case should produce a structured trace whenever the integration can expose the underlying events. The trace is the evidence layer behind evaluation results.

## Canonical event types

- run_started

- workflow_started

- llm_call

- llm_response

- tool_call

- tool_result

- state_change

- workflow_step_started

- workflow_step_completed

- final_response

- error

- run_completed

## Trace event shape

> {  
> "event_id": "evt_123",  
> "run_case_id": "case_456",  
> "timestamp": "...",  
> "type": "tool_call",  
> "node_id": "refund_tool",  
> "payload": {...},  
> "duration_ms": 84,  
> "parent_event_id": "evt_122"  
> }

## Evidence linking

Metric results should link back to the trace events that caused the decision. For example, a “refund eligibility guard” failure should point to the refund call and the prior eligibility-check event. This makes the result auditable and enables automated failure explanations.

# 13. Evaluation Result Model

## Metric result

> MetricResult  
> ├── metric_id  
> ├── test_case_id  
> ├── status: PASS / FAIL / ERROR / SKIPPED  
> ├── score  
> ├── normalized_score  
> ├── raw_value  
> ├── threshold  
> ├── reason  
> ├── evidence_event_ids\[\]  
> └── evaluator_version

## Run summary

A run aggregates test-level and metric-level results, but should retain raw results so dashboards can be recomputed without rerunning the agent.

| **Metric**           | **Value** | **Pass rule** | **Evidence**        |
|----------------------|-----------|---------------|---------------------|
| Tool output accuracy | 97.2%     | \>= 95%       | 486/500 test cases  |
| Workflow compliance  | 91.3%     | \>= 95%       | 43 trace violations |
| Latency p95          | 2.4s      | \<= 3.0s      | Run metrics         |
| Resolution quality   | 0.88      | \>= 0.85      | LLM judge v2        |

## Failure clustering

The system should group failures by shared causes or patterns. Clustering can use structured metadata first, then embeddings and LLM summarization as a secondary analytical layer. The cluster itself is an analysis artifact and should point to its constituent failures.

# 14. Dashboard and HTML Preview System

## Dashboard principle

Dashboards are rendered from structured definitions, not arbitrary LLM-generated frontend source. The LLM selects from a known component vocabulary, binds components to queryable metrics, and proposes a layout. The frontend validates and renders the definition.

## Dashboard component catalog

- Metric card

- Trend chart

- Metric comparison

- Failure breakdown

- Test case table

- Trace viewer

- Run comparison

- Filter control

- Distribution chart

- Coverage matrix

- Regression alert

- Text / insight panel

## Preview lifecycle

> Evaluation Spec  
> ↓  
> Mock Dataset + Synthetic Results  
> ↓  
> Dashboard Definition  
> ↓  
> HTML Preview  
> ↓  
> User feedback  
> ↓  
> Updated Definition  
> ↓  
> User confirmation  
> ↓  
> Real Evaluation Run

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>Important UX rule</strong></p>
<p>The preview must clearly identify mock data. It validates the evaluation design and presentation; it is not a prediction of the eventual score.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## Custom dashboards

Users can ask natural-language requests such as “Create a dashboard focused on tool failures and latency regressions.” The LLM converts the request into a dashboard definition using the supported component schema. Advanced users can edit the layout and component bindings directly.

# 15. LLM Evaluation Harness

## Responsibilities

- Understand project and workflow context

- Explain what the agent does

- Propose evaluation objectives and metrics

- Generate and revise evaluation specifications

- Generate and critique test cases

- Invoke platform tools to run evaluations

- Analyze failures and summarize evidence

- Generate dashboard definitions

- Maintain conversational context across evaluation iterations

## Harness tools

> read_file()  
> search_code()  
> get_project_graph()  
> get_workflows()  
> get_workflow_trace_schema()  
> create_evaluation()  
> update_evaluation()  
> validate_evaluation()  
> generate_tests()  
> validate_dataset()  
> start_run()  
> get_run_status()  
> get_metric_results()  
> get_failed_cases()  
> get_trace()  
> create_dashboard()  
> update_dashboard()

## Tool-driven behavior

The harness should never “pretend” that an action succeeded. Mutating or execution tools return explicit structured states such as draft, validated, queued, running, completed, failed, or rejected. The conversation layer renders those states to the user.

# 16. Skills Architecture

A SKILL.md-style capability layer should make the harness extensible. Skills describe reusable knowledge and operating procedures rather than storing core state.

> skills/  
> ├── code-analysis/  
> │ └── SKILL.md  
> ├── evaluation-design/  
> │ └── SKILL.md  
> ├── test-generation/  
> │ └── SKILL.md  
> ├── trace-analysis/  
> │ └── SKILL.md  
> ├── custom-evaluators/  
> │ └── SKILL.md  
> ├── llm-judge/  
> │ └── SKILL.md  
> └── dashboard/  
> ├── SKILL.md  
> └── components/

## Skill contract

| **Field**   | **Purpose**                          |
|-------------|--------------------------------------|
| Purpose     | What the skill accomplishes.         |
| When to use | Triggering conditions.               |
| Inputs      | Required context or objects.         |
| Outputs     | Expected structured result.          |
| Tools       | Which platform tools may be invoked. |
| Rules       | Constraints and safety requirements. |
| Examples    | Representative patterns.             |

## Dashboard skill

The dashboard skill should describe the component vocabulary, layout rules, supported data bindings, interaction patterns, and examples. It should not contain user-specific result data. The actual dashboard renderer remains a deterministic frontend capability.

# 17. Backend APIs and Service Contracts

## Project APIs

> POST /projects  
> POST /projects/{id}/sources  
> GET /projects/{id}  
> GET /projects/{id}/workflows  
> GET /projects/{id}/graph

## Evaluation APIs

> POST /workflows/{id}/evaluations  
> GET /evaluations/{id}  
> POST /evaluations/{id}/versions  
> POST /evaluations/{id}/validate  
> POST /evaluations/{id}/preview

## Dataset APIs

> POST /datasets  
> POST /datasets/{id}/cases  
> POST /datasets/{id}/generate  
> POST /datasets/{id}/validate  
> GET /datasets/{id}/versions

## Run APIs

> POST /evaluations/{id}/runs  
> GET /runs/{id}  
> GET /runs/{id}/progress  
> GET /runs/{id}/results  
> GET /runs/{id}/failures  
> GET /runs/{id}/cases/{case_id}/trace

## Dashboard APIs

> POST /dashboards  
> GET /dashboards/{id}  
> POST /dashboards/{id}/versions  
> POST /dashboards/{id}/validate

The API should use stable object IDs, optimistic concurrency or version checks for mutable definitions, and explicit state transitions for long-running operations.

# 18. Persistence and Storage Design

## PostgreSQL

PostgreSQL should remain the source of truth for structured application data. pgvector should be used selectively for semantic retrieval of code chunks, prior failures, evaluation discussions, test examples, and related artifacts.

## Recommended initial tables

| **Table**           | **Purpose**                         |
|---------------------|-------------------------------------|
| projects            | Workspace/project metadata          |
| project_sources     | ZIP/repository/path source metadata |
| agents              | Detected agents                     |
| workflows           | Detected executable workflows       |
| workflow_nodes      | Structured workflow graph           |
| tools               | Tool definitions and references     |
| evaluations         | Stable evaluation identity          |
| evaluation_versions | Immutable evaluation definitions    |
| metrics             | Stable metric identity              |
| metric_versions     | Immutable metric configuration      |
| evaluators          | Evaluator implementations           |
| datasets            | Dataset identity                    |
| dataset_versions    | Immutable dataset snapshots         |
| test_cases          | Versioned test data                 |
| runs                | Execution records                   |
| run_cases           | Per-test execution status           |
| traces              | Trace metadata                      |
| trace_events        | Trace event stream                  |
| metric_results      | Per-test metric results             |
| dashboards          | Dashboard identity                  |
| dashboard_versions  | Renderable dashboard definitions    |
| artifacts           | Large generated or uploaded files   |

## Object storage

Large ZIPs, logs, traces, generated datasets, HTML previews, and other artifacts should move to object storage rather than bloating PostgreSQL. PostgreSQL stores metadata and references.

# 19. Security, Isolation, and Trust

## Threat model

Users may upload arbitrary or untrusted code. The system must assume that project code can be malicious, buggy, resource-intensive, or designed to exfiltrate secrets.

## Required controls

- Sandbox every code execution job.

- Disable outbound network access by default.

- Never expose platform credentials to user containers.

- Use short-lived credentials for explicitly approved integrations.

- Enforce CPU, memory, disk, process, and wall-clock limits.

- Scan uploaded archives for path traversal and malicious file names before extraction.

- Use separate identities for control-plane services and execution workers.

- Encrypt stored artifacts and database connections.

- Audit evaluation creation, execution, configuration mutation, and secret access.

- Apply workspace-level authorization to every object and execution request.

## Execution trust boundary

> Control Plane  
> │  
> │ validated job + ephemeral token  
> ▼  
> Isolated Worker  
> │  
> ├── user code  
> ├── sandboxed filesystem  
> └── restricted network  
> │  
> ▼  
> Artifact / result boundary

# 20. Versioning, Reproducibility, and Governance

A result is trustworthy only when the system can reconstruct the conditions under which it was produced. Every run should store immutable references to all meaningful inputs.

| **Dimension** | **Stored reference**                   |
|---------------|----------------------------------------|
| Agent         | Commit, artifact hash, or image digest |
| Workflow      | Workflow version / graph hash          |
| Evaluation    | Evaluation version ID                  |
| Metrics       | Metric version IDs                     |
| Evaluator     | Evaluator implementation version       |
| Dataset       | Dataset version ID                     |
| Judge         | Rubric + model version if used         |
| Runtime       | Container image, runtime configuration |
| Dashboard     | Dashboard version used for display     |

## Governance states

> DRAFT → VALIDATED → APPROVED → EXECUTABLE → RETIRED

Not every deployment needs all states, but the data model should permit them. A production evaluation may require approval before it can run automatically.

# 21. Regression, Comparison, and Continuous Evaluation

## Run comparison

Users should be able to compare two runs or versions using the same evaluation specification. The comparison should show changes at metric, test, and failure-cluster levels.

> Agent v12 → Run \#42 → 92.1%  
> Agent v13 → Run \#43 → 86.4%  
>   
> Regression: -5.7 points  
> Largest change: Refund Policy Compliance -12.4 points

## Regression rules

- Absolute threshold: metric must remain above X.

- Delta threshold: metric cannot decline by more than Y points.

- Failure budget: no more than N critical failures.

- Category regression: no material degradation in a specific test category.

- Workflow invariant: zero violations of critical trace rules.

## CI/CD integration

Later, the evaluation API can be invoked from a CI pipeline. A candidate agent build runs a required evaluation suite, and the pipeline fails when declared regression rules are violated.

# 22. Failure Analysis and Root-Cause Workflow

## Failure investigation flow

> Metric failure  
> ↓  
> Identify failed test  
> ↓  
> Open trace  
> ↓  
> Collect linked evidence events  
> ↓  
> Cluster with similar failures  
> ↓  
> Generate explanation  
> ↓  
> Suggest likely fix / new test  
> ↓  
> User confirms and updates evaluation

## LLM role in failure analysis

LLMs are particularly valuable after deterministic evaluation has already identified the failure. The model can summarize a trace, compare the actual path with the expected path, group similar failures, and propose additional test cases. The LLM explanation should cite or link to structured evidence rather than inventing facts.

## Suggested remediation loop

- Failure detected

- Root-cause summary generated

- User reviews evidence

- New regression test proposed

- Metric/evaluator updated if necessary

- Evaluation rerun

- Regression closes when the failure no longer reproduces

# 23. Functional Requirements

| **ID** | **Area**                           | **Requirement**                                                                                                                   |
|--------|------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| FR-01  | Project ingestion                  | System shall ingest an agent codebase from a ZIP for MVP and preserve source metadata.                                            |
| FR-02  | Workflow discovery                 | System shall identify candidate agentic workflows and present them as evaluation targets.                                         |
| FR-03  | Code retrieval                     | Evaluation harness shall retrieve relevant code and project context without loading the entire codebase into every model context. |
| FR-04  | Natural-language evaluation design | User shall be able to describe an evaluation goal conversationally.                                                               |
| FR-05  | Structured evaluation              | System shall translate intent into a versioned structured EvaluationSpec.                                                         |
| FR-06  | Metric creation                    | User shall create metrics targeting input, model output, tool calls, tool outputs, state, workflow, trace, or run data.           |
| FR-07  | Evaluator selection                | Metric shall reference a configurable evaluator type.                                                                             |
| FR-08  | Custom code                        | Authorized users shall create custom code evaluators in a sandbox.                                                                |
| FR-09  | Expected values                    | Test cases shall support structured expected values and references.                                                               |
| FR-10  | Test generation                    | System shall generate and validate test suites using LLM assistance.                                                              |
| FR-11  | Preview                            | System shall render a mock HTML/dashboard preview from an evaluation definition.                                                  |
| FR-12  | Execution                          | System shall run evaluation cases in isolated containers.                                                                         |
| FR-13  | Tracing                            | System shall collect structured execution traces where integration support exists.                                                |
| FR-14  | Scoring                            | System shall produce metric-level, case-level, and run-level results.                                                             |
| FR-15  | Evidence                           | Metric results shall link to supporting trace evidence when applicable.                                                           |
| FR-16  | Dashboard                          | System shall render configurable dashboards from structured definitions.                                                          |
| FR-17  | Comparison                         | System shall compare evaluation runs and agent versions.                                                                          |
| FR-18  | Versioning                         | System shall version evaluations, datasets, evaluators, and relevant agent artifacts.                                             |
| FR-19  | Audit                              | System shall log security-sensitive mutations and execution actions.                                                              |

# 24. Non-Functional Requirements

## Reliability

- Evaluation definitions must validate before execution.

- Run state must survive API restarts.

- Partial worker failures must produce explicit case errors rather than silently disappearing.

## Performance

- Project analysis should be incremental.

- Evaluation execution should support parallel cases subject to quotas.

- Dashboard queries must be pre-aggregatable for large runs.

## Scalability

- Worker execution should be horizontally scalable.

- Trace storage must support high event volume without coupling dashboard latency to raw event scans.

## Security

- Sandbox all untrusted code.

- Enforce authorization at every API boundary.

- Minimize secret exposure.

## Usability

- A user can create a first evaluation without learning a DSL.

- Every score has an inspectable path to underlying evidence.

## Extensibility

- New evaluator types can be added without changing metric concepts.

- New dashboard components can be added without changing stored result data.

# 25. Reference Implementation Structure

> repo/  
> ├── apps/  
> │ ├── web/  
> │ └── api/  
> ├── packages/  
> │ ├── evaluation-schema/  
> │ ├── evaluator-sdk/  
> │ ├── trace-schema/  
> │ └── dashboard-schema/  
> ├── services/  
> │ ├── project-understanding/  
> │ ├── run-orchestrator/  
> │ ├── evaluator-runtime/  
> │ └── worker/  
> ├── skills/  
> │ ├── code-analysis/  
> │ ├── evaluation-design/  
> │ ├── test-generation/  
> │ ├── trace-analysis/  
> │ └── dashboard/  
> ├── migrations/  
> ├── infra/  
> │ ├── docker/  
> │ └── compose/  
> └── tests/

## MVP deployment topology

> Browser  
> │  
> ▼  
> Next.js / React  
> │  
> ▼  
> FastAPI API  
> ├── PostgreSQL + pgvector  
> ├── Redis / queue  
> └── Worker(s)  
> └── Docker evaluation containers

Keep the initial deployment as a modular monolith plus worker tier. Introduce service boundaries only when scale, security, or operational ownership warrants them.

# 26. Implementation Roadmap

| **Phase**                    | **Scope**                                                                                              |
|------------------------------|--------------------------------------------------------------------------------------------------------|
| Phase 0 — Foundations        | Project ingestion, auth, Postgres schema, artifact storage, basic workflow metadata.                   |
| Phase 1 — Code understanding | Static analysis, code retrieval, workflow graph, LLM project explanation.                              |
| Phase 2 — Evaluation builder | Chat, EvaluationSpec schema, metric builder, evaluator registry, validation.                           |
| Phase 3 — Test system        | Dataset management, test generation, validation, expected-value support.                               |
| Phase 4 — Execution          | Docker runner, job queue, case execution, basic trace schema.                                          |
| Phase 5 — Evaluation engine  | Deterministic evaluators, custom code evaluator, reference evaluator, LLM judge as optional evaluator. |
| Phase 6 — Results            | Metric results, failure views, trace explorer, run summaries.                                          |
| Phase 7 — Dashboard          | Fixed component catalog, mock preview, configurable dashboard definitions.                             |
| Phase 8 — Intelligence       | Failure clustering, root-cause summaries, automatic regression test suggestions.                       |
| Phase 9 — Scale              | Git integration, CI/CD, scheduled runs, replay, multi-workflow parallelism, stronger sandboxing.       |

## MVP success criteria

- A real agent repository can be ingested and understood enough to identify one usable workflow.

- A user can define a custom metric in natural language and review the resulting structured specification.

- At least deterministic, reference, custom-code, and optional LLM-based evaluators work end-to-end.

- The agent executes in Docker and produces traces.

- A run produces metric results that link to evidence.

- A preview dashboard can be generated before a real run.

- Two agent versions can be compared using the same evaluation.

# 27. Testing and Quality Strategy

## Unit tests

- Metric schema validation

- Evaluator implementations

- Trace parsing

- Score aggregation

- Dashboard definition validation

- Version compatibility

## Integration tests

- ZIP ingestion to workflow discovery

- Evaluation creation to run submission

- Docker execution to trace persistence

- Metric evaluation to result aggregation

- Dashboard definition to renderable page

## Evaluation engine self-evaluation

The platform itself should have evaluations. For example, evaluator implementations should be tested against known fixture traces. Dashboard generation should be validated against schema fixtures. The LLM harness should be evaluated on whether its proposed definitions satisfy schema and intent constraints.

## Golden fixtures

Maintain a collection of representative agent traces and expected metric results. These become regression fixtures for new evaluator versions and execution changes.

# 28. Key Risks and Mitigations

| **Risk**                           | **Why it matters**                                | **Mitigation**                                                                               |
|------------------------------------|---------------------------------------------------|----------------------------------------------------------------------------------------------|
| LLM generates incorrect evaluation | A wrong metric can create false confidence.       | Structured schema, preview, validation, human confirmation, evaluator tests.                 |
| Bad synthetic tests                | The system may “evaluate the wrong thing.”        | Test validation, expected-value provenance, coverage and manual review.                      |
| Untrusted code execution           | Uploaded code may compromise infrastructure.      | Sandboxing, no network by default, resource quotas, isolated identities.                     |
| Unreproducible scores              | Users cannot explain changes between runs.        | Version all inputs, evaluators, datasets, models, and agent artifacts.                       |
| Too much dashboard flexibility     | Arbitrary UI generation can become fragile.       | Schema-based component system with deterministic renderer.                                   |
| Expensive LLM use                  | Code analysis and judges can become costly.       | Incremental retrieval, caching, deterministic evaluators first, model policy controls.       |
| Framework fragmentation            | Every agent framework exposes different signals.  | Define a canonical trace and adapter model.                                                  |
| Metric sprawl                      | Users can create hundreds of meaningless metrics. | Metric grouping, ownership, descriptions, thresholds, health checks, and reusable templates. |

# 29. Future Capabilities

## Continuous evaluation

Run required suites automatically on new commits, datasets, or configuration changes.

## Production monitoring bridge

Link observed production traces to the same evaluation definitions used pre-release.

## Evaluation marketplace

Share reusable evaluators, metric templates, rubrics, and dashboard packs.

## Cross-agent benchmarking

Compare multiple agents against the same evaluation suite.

## Adaptive test generation

Generate new tests from newly discovered failure modes and uncovered workflow branches.

## Human review queues

Route uncertain or high-impact cases to humans and feed approved labels back into datasets.

## Policy engine

Enforce organization-wide minimum evaluation requirements before deployment.

## Multi-agent workflow evaluation

Evaluate handoffs, delegation, shared state, and coordination across agent teams.

## Evaluation optimization

Select smaller/cheaper evaluators for cases where deterministic checks are sufficient.

# 30. Final Product Blueprint

## The product should feel like this

> Bring an agent  
> ↓  
> The platform understands the project  
> ↓  
> Choose a workflow  
> ↓  
> Tell the evaluation harness what should be true  
> ↓  
> Harness proposes structured metrics + tests  
> ↓  
> User reviews the evaluation  
> ↓  
> System previews the evaluation experience  
> ↓  
> Run the real agent in an isolated environment  
> ↓  
> Collect full traces  
> ↓  
> Run configurable evaluators  
> ↓  
> Produce transparent metric results  
> ↓  
> Inspect failures and evidence  
> ↓  
> Compare versions  
> ↓  
> Improve the agent and rerun

## Core capability statement

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>What this product is</strong></p>
<p>A conversational, configurable evaluation engine for agentic workflows. The LLM makes evaluation authoring and analysis accessible; the underlying engine executes and scores explicit evaluation definitions using deterministic checks, custom logic, references, trace rules, and optional semantic judges.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## What this product is not

- Not only an LLM-as-a-judge API.

- Not only a dashboarding tool.

- Not a generic agent runtime.

- Not a static test generator.

- Not a replacement for domain-specific correctness definitions.

## North-star workflow

A user should be able to describe a business or engineering expectation in plain language, see the exact executable metric and test definition that the system derived, verify it with a mock preview, execute it against their real agent, and trace every score back to concrete evidence. That is the core product experience around which the architecture, UI, evaluation model, and execution system should be built.

# Appendix A. Example Evaluation Specification

> evaluation:  
> id: refund-safety  
> name: Refund Safety  
> target:  
> workflow: refund_workflow  
>   
> dataset:  
> id: refund-tests-v8  
>   
> metrics:  
> - id: eligibility_guard  
> name: Eligibility Check Required  
> target:  
> type: trace  
> evaluator:  
> type: trace_rule  
> condition: \|  
> every(refund_order) has prior(check_refund_eligibility)  
> and prior.check_refund_eligibility.output.eligible == true  
> scoring:  
> type: binary  
> threshold: 1.0  
>   
> - id: refund_amount  
> name: Refund Amount Accuracy  
> target:  
> type: tool_output  
> tool: refund_order  
> evaluator:  
> type: numeric  
> actual: output.amount  
> expected: test.expected.refund_amount  
> tolerance: 0  
> scoring:  
> type: binary  
> threshold: 1.0  
>   
> - id: tool_arguments  
> name: Refund Tool Arguments  
> target:  
> type: tool_arguments  
> tool: refund_order  
> evaluator:  
> type: rule  
> expression: \|  
> args.order_id == expected.order_id  
> and args.amount \<= expected.max_allowed_amount  
> scoring:  
> type: binary  
> threshold: 1.0  
>   
> - id: final_explanation  
> name: Customer Explanation Quality  
> target:  
> type: final_response  
> evaluator:  
> type: llm_judge  
> rubric_ref: refund-explanation-v2  
> scoring:  
> type: numeric  
> range: \[0,1\]  
> threshold: 0.85  
>   
> regression_policy:  
> - metric: eligibility_guard  
> max_allowed_regression: 0  
> - metric: refund_amount  
> max_allowed_regression: 0  
> - metric: final_explanation  
> max_allowed_regression: 0.05

# 31. Detailed Agent and Harness Design

This section defines the internal agent that powers the conversational evaluation experience. The agent is not the evaluation engine itself. It is an orchestration and reasoning layer that understands project context, discovers available capabilities, converts user intent into structured evaluation objects, invokes approved tools, starts evaluation jobs, and explains results. All executable decisions that affect infrastructure, persistence, or scoring must pass through validated platform APIs and schemas.

## 31.1 Agent responsibilities

- Understand the uploaded or connected project without requiring the user to manually describe its architecture.

- Build and maintain a structured representation of agents, workflows, tools, files, dependencies, entrypoints, configuration, and execution requirements.

- Use search and inspection tools to retrieve only the code and artifacts relevant to the current task.

- Discover evaluation opportunities and translate natural-language requirements into explicit evaluation use cases.

- Create and modify evaluation specifications, datasets, metrics, evaluators, test cases, thresholds, and dashboard definitions through typed tools.

- Validate evaluation definitions before execution and surface ambiguity rather than silently inventing assumptions.

- Provision evaluation runs through the execution service rather than executing arbitrary code directly from the conversational layer.

- Read traces and results, identify failures and regressions, and explain findings with evidence links.

- Generate or modify evaluation UI through an approved component/plugin system rather than arbitrary frontend code.

## 31.2 Agent architecture

User Chat  
↓  
Conversation / Session Manager  
↓  
Harness Planner  
├── Project Context Retriever  
├── Code / Artifact Search  
├── Workflow Reasoner  
├── Evaluation Designer  
├── Test / Use-Case Generator  
├── Execution Orchestrator  
├── Result / Failure Analyst  
└── UI / Dashboard Composer  
↓  
Typed Tool Layer  
↓  
Platform Services / APIs

The harness may use one or more LLM calls internally, but the user should experience a single coherent agent. Internally, planner state should identify the current task, required context, proposed actions, tool calls, pending confirmations, and resulting object IDs.

## 31.3 Conversation state and context hierarchy

The agent should not rely on a single giant context window. Context should be assembled in layers, with cheap and high-signal information loaded first and deep content fetched only when necessary.

Layer 0: Session context  
Current user request, selected project/workflow, recent messages  
  
Layer 1: Project summary  
Project metadata, detected framework, agents, workflows, tools  
  
Layer 2: Workflow context  
Entrypoint, graph, relevant files, prompts, tool contracts  
  
Layer 3: Task-specific evidence  
Search results, code excerpts, test cases, traces, prior evaluations  
  
Layer 4: Execution evidence  
Live run state, metric results, failed cases, linked trace events

## 31.4 Planning loop

1\. Interpret user intent  
2. Identify target object(s)  
3. Determine missing information  
4. Search / retrieve required context  
5. Form a structured plan  
6. Propose mutations or execution  
7. Validate inputs against schemas and policies  
8. Ask for confirmation when the action is consequential  
9. Execute via typed tools  
10. Observe result  
11. Explain result and persist relevant state

Confirmation should be required before actions that materially consume resources, modify an approved evaluation, run user code at scale, access an explicitly connected external system, or publish a dashboard/configuration. Safe read-only analysis can happen automatically.

# 32. Project Parsing, Ingestion, and Understanding

## 32.1 Supported project inputs

- ZIP archive for MVP.

- Git repository or repository URL as the next integration.

- Local or mounted filesystem path for development environments.

- Future connectors for cloud storage, artifact registries, or supported agent platforms.

## 32.2 Ingestion pipeline

Source  
↓  
Archive / path validation  
↓  
File inventory  
↓  
Ignore / include policy  
↓  
Language + framework detection  
↓  
Dependency extraction  
↓  
AST / syntax indexing  
↓  
Entrypoint + agent candidate detection  
↓  
Tool / schema / prompt discovery  
↓  
LLM semantic analysis  
↓  
Project Knowledge Graph  
↓  
Search index + persistent project snapshot

## 32.3 Parsing responsibilities

The parser should extract deterministic facts before asking the LLM to interpret semantics. This reduces hallucination risk and token usage.

- File paths, file types, language, framework, package manifests, and dependency versions.

- Functions, classes, exported symbols, entrypoints, imports, and references.

- Agent loops, graph definitions, workflow nodes, prompts, tool registrations, schemas, and adapters where discoverable.

- Environment-variable references and external service dependencies without exposing secret values.

- Potential execution commands, test commands, and package installation requirements.

- Known observability hooks and the points at which trace instrumentation can be inserted.

## 32.4 Semantic understanding

The LLM receives parsed facts plus targeted code excerpts and builds a semantic model. It should identify not only what code exists, but why each component matters to an agent workflow.

Project Model  
├── agents\[\]  
│ ├── identity  
│ ├── entrypoint  
│ ├── model  
│ ├── instructions  
│ └── tools\[\]  
├── workflows\[\]  
│ ├── nodes\[\]  
│ ├── transitions\[\]  
│ ├── inputs  
│ ├── outputs  
│ └── invariants\[\]  
├── tools\[\]  
│ ├── schema  
│ ├── side_effects  
│ └── dependencies\[\]  
└── artifacts\[\]

## 32.5 Project knowledge graph

The project knowledge graph becomes the primary semantic representation used by the harness. Every discovered fact should retain provenance back to a file, symbol, line range, or integration metadata where possible.

Workflow → Node → Tool → Tool Schema  
Workflow → Agent → Prompt / Model  
Workflow → Input / Output Schema  
Tool → External Dependency  
Evaluation → Workflow / Metric / Dataset  
Run → Evaluation Version / Agent Version  
Metric Result → Test → Trace Event → Source Evidence

## 32.6 Incremental re-parsing

Re-reading the entire repository for every chat message is inefficient. Store a content hash for each indexed file and invalidate only affected parse and semantic-analysis nodes when files change. A project snapshot should therefore be reproducible from a source revision plus parser/harness versions.

# 33. Search and Retrieval Tool System

## 33.1 Why the harness needs search tools

The agent cannot reason reliably about an arbitrary codebase from the initial prompt alone. It needs controlled tools for locating relevant evidence. Search should be a first-class capability rather than an implicit vector retrieval mechanism.

## 33.2 Core search tools

- search_files(query, filters): semantic and keyword search over indexed files and artifacts.

- find_symbol(name): locate functions, classes, tool definitions, workflow nodes, and entrypoints.

- read_file(path, range): retrieve a bounded source region.

- search_code(pattern): exact or structural code search.

- search_project(query): search across project metadata and the knowledge graph.

- get_workflow(workflow_id): return the structured workflow model.

- get_tool(tool_id): return tool contract, schema, source references, and side-effect classification.

- search_evaluations(query): find previous metrics, test cases, and evaluation versions.

- search_traces(query): find similar historical failures or traces when permitted.

- search_docs(query): retrieve project documentation and reference material.

## 33.3 Retrieval policy

- Prefer deterministic search when the user references an exact symbol, path, or identifier.

- Use semantic search when intent is conceptual, such as “find where refunds are validated.”

- Retrieve the smallest useful source window and expand only when necessary.

- Return provenance with every result so the agent can cite what it used.

- Do not let retrieval silently cross project or workspace boundaries.

- Do not expose secrets, credential material, or restricted files through generic search.

## 33.4 Search result contract

SearchResult  
├── result_id  
├── source_type  
├── project_id  
├── path / object_id  
├── snippet  
├── line_range  
├── relevance  
├── provenance  
└── access_policy

## 33.5 Retrieval strategy

The recommended strategy is hybrid retrieval: lexical search for exact identifiers, structural indexes for symbols and relationships, and embeddings for conceptual queries. pgvector can support semantic search over code summaries and historical evaluation artifacts, while PostgreSQL remains the system of record.

# 34. Evaluation Use-Case Discovery

## 34.1 From workflow understanding to evaluation opportunities

After the project is understood, the harness should automatically propose evaluation use cases. A use case is a concrete statement of behavior that is worth measuring, together with the observable evidence and likely evaluation strategy.

Workflow understanding  
↓  
Behavior inventory  
↓  
Risk / importance analysis  
↓  
Evaluation use-case candidates  
↓  
Metric / evaluator suggestions  
↓  
User review  
↓  
Evaluation specification

## 34.2 Sources for use-case discovery

- Workflow nodes and branches.

- Tool contracts and required arguments.

- Business rules encoded in prompts or code.

- Input/output schemas.

- Error handling and fallback paths.

- State transitions and side effects.

- Existing automated tests.

- Project documentation and product requirements.

- Known failure traces from previous runs.

- User instructions given during evaluation design.

## 34.3 Example use-case generation

For a refund workflow, the system might propose the following use cases:

- Refund is issued only after eligibility has been verified.

- The refund tool receives the correct order ID.

- The refund amount matches the calculated eligible amount.

- The agent escalates cases that violate refund policy.

- The final response accurately describes what action actually occurred.

- The workflow terminates successfully within the allowed latency budget.

## 34.4 Use-case object

EvaluationUseCase  
├── id  
├── name  
├── intent  
├── target  
├── business_importance  
├── risk_level  
├── observable_evidence\[\]  
├── candidate_metrics\[\]  
├── candidate_evaluators\[\]  
├── required_test_data\[\]  
└── provenance\[\]

## 34.5 Use-case review experience

The UI should present proposed use cases as editable cards. Each card should explain what is being evaluated, why the harness believes it matters, what evidence would be inspected, and which evaluator type is proposed. The user can accept, modify, combine, or reject the candidate before it becomes a metric.

# 35. Evaluation Creation and Spin-Up Orchestration

## 35.1 Evaluation creation flow

User intent  
↓  
Harness searches relevant project context  
↓  
Use-case candidates  
↓  
Metric / evaluator definitions  
↓  
Dataset requirements  
↓  
Structured EvaluationSpec  
↓  
Schema + policy validation  
↓  
Preview  
↓  
Approval  
↓  
Evaluation version published  
↓  
Run request

## 35.2 Evaluation spin-up

“Spin up an evaluation” means converting a validated EvaluationSpec into an executable run plan. The run plan resolves the exact agent revision, dataset revision, evaluator versions, resource limits, environment policy, instrumentation configuration, and concurrency settings.

EvaluationSpec  
↓  
Run Planner  
├── resolve agent artifact  
├── resolve dataset version  
├── resolve evaluator versions  
├── create execution image / select cached image  
├── allocate worker capacity  
├── create trace namespace  
└── create run manifest  
↓  
Run Orchestrator  
↓  
Parallel test execution  
↓  
Result aggregation

## 35.3 Run manifest

run_manifest:  
agent_revision: commit_123  
evaluation_version: eval_7  
dataset_version: ds_14  
evaluator_versions:  
tool_accuracy: evaluator_3  
policy_check: evaluator_5  
runtime:  
cpu: 2  
memory_mb: 4096  
timeout_seconds: 120  
network_policy: restricted  
concurrency: 20

## 35.4 Run lifecycle

DRAFT → QUEUED → PROVISIONING → RUNNING → AGGREGATING → COMPLETE  
↘ FAILED / CANCELLED  
  
Per test case:  
QUEUED → RUNNING → SCORING → PASS / FAIL / ERROR / SKIPPED

## 35.5 Parallelism and partial failure

Test cases should be independently schedulable so a failure in one case does not abort an entire run unless the user or policy explicitly requests fail-fast behavior. The run should track worker-level errors, infrastructure errors, agent errors, evaluator errors, and assertion failures separately.

# 36. Agent Tool Registry and Action Model

## 36.1 Tool categories

- Read tools: inspect code, metadata, traces, evaluations, datasets, and documentation.

- Planning tools: create or update structured objects in draft state.

- Execution tools: submit, inspect, cancel, or retry evaluation runs.

- Analysis tools: aggregate results, compare runs, cluster failures, and inspect traces.

- UI tools: create or modify dashboards, previews, and report layouts.

- External tools: future connectors to repositories, CI systems, issue trackers, or storage.

## 36.2 Tool registry

Every harness tool should be registered with a typed schema, access policy, side-effect classification, timeout, and audit policy.

ToolDefinition  
├── name  
├── description  
├── input_schema  
├── output_schema  
├── read_only / mutating  
├── required_scope  
├── confirmation_policy  
├── timeout  
└── audit_event_type

## 36.3 Tool selection

The LLM should not invent tool names. At planning time, the harness receives a registry subset appropriate to the current task. Tool calls are schema-validated before execution and the result is returned to the model with a stable reference.

# 37. UI Generation, Plugin, and Skill Architecture

## 37.1 Why UI generation should be constrained

The harness should be able to create a workflow-specific evaluation experience without being allowed to inject arbitrary frontend code into the application. The UI layer therefore needs a declarative component system and plugin boundary.

## 37.2 UI layers

LLM intent  
↓  
Dashboard / UI specification  
↓  
Schema validator  
↓  
Component registry  
↓  
Renderer  
↓  
Interactive evaluation UI

## 37.3 UI component registry

- MetricCard

- MetricBreakdown

- RunSummary

- RunComparison

- FailureCluster

- TestCaseTable

- TraceTimeline

- ToolCallInspector

- ExpectedVsActual

- TimeSeries

- DistributionChart

- FilterBar

- WorkflowGraph

- EvaluationPlan

- ApprovalPanel

- HTMLPreview

## 37.4 Dashboard Skill

The Dashboard Skill should describe available components, supported data bindings, layout rules, interaction patterns, and constraints. It should teach the harness how to compose a useful interface from registered components.

SKILL.md  
├── purpose  
├── when_to_use  
├── available_components  
├── component_inputs  
├── layout_patterns  
├── supported_interactions  
├── data_binding_rules  
├── examples  
└── safety_constraints

## 37.5 Plugin model

A plugin should extend the platform through declared capabilities rather than arbitrary UI injection. Examples include a custom metric evaluator plugin, an agent framework adapter, a dashboard component pack, or a repository connector.

PluginManifest  
├── plugin_id  
├── version  
├── capabilities\[\]  
├── tools\[\]  
├── evaluators\[\]  
├── ui_components\[\]  
├── permissions\[\]  
└── compatibility

## 37.6 Agent framework adapters

Different agent frameworks expose different runtime and trace models. A framework adapter should normalize those differences into the platform’s canonical interfaces: entrypoint, invoke(), trace events, tool calls, state transitions, and artifacts.

Framework Adapter  
↓  
Canonical Agent Interface  
├── discover()  
├── validate()  
├── prepare_runtime()  
├── invoke(test_input)  
└── collect_trace()

## 37.7 UI plugin lifecycle

Discover plugin  
↓  
Validate manifest + permissions  
↓  
Register schemas/components  
↓  
Expose capability to harness  
↓  
LLM composes UI definition  
↓  
Renderer instantiates approved components

# 38. End-to-End Agent Interaction Example

The following sequence shows how all of the above pieces work together in a single user session.

USER  
“Evaluate my refund agent. I want to make sure it only issues refunds  
when the order is eligible, and that the refund amount is correct.”  
  
HARNESS  
1. Identify selected project and refund workflow.  
2. Search workflow graph, refund tools, eligibility code, schemas, and tests.  
3. Create use cases:  
- Eligibility must be checked before refund.  
- Refund amount must equal eligible amount.  
- Correct order ID must be used.  
4. Propose metrics and evaluators.  
5. Ask user to confirm / edit.  
  
USER  
“Also check that failed eligibility cases are escalated.”  
  
HARNESS  
6. Add escalation use case and trace-based metric.  
7. Generate a structured EvaluationSpec.  
8. Generate or select dataset cases.  
9. Show HTML mock dashboard with proposed metrics.  
  
USER  
“Run 500 cases.”  
  
PLATFORM  
10. Freeze evaluation version.  
11. Resolve agent + dataset + evaluator versions.  
12. Allocate Docker workers.  
13. Execute 500 cases in parallel.  
14. Collect traces and evaluate each metric.  
15. Aggregate scores and cluster failures.  
16. Render dashboard.  
  
HARNESS  
17. Explain the dominant failures with trace evidence.  
18. Offer a new regression test for the largest failure cluster.

# 39. Agent Failure Handling and Recovery

- Unknown target: ask the user to select a project/workflow rather than guessing.

- Insufficient context: search more evidence before proposing an evaluation.

- Ambiguous requirement: represent the ambiguity explicitly and request a decision before freezing the EvaluationSpec.

- Invalid metric: return schema/type validation errors and propose a corrected definition.

- Tool failure: preserve the tool error, retry only when policy permits, and never fabricate success.

- Execution failure: distinguish agent failure from infrastructure failure.

- LLM failure: allow deterministic platform operations and existing evaluation runs to remain usable without the conversational agent.

- Plugin failure: disable the affected capability without compromising core evaluation data.

# 40. Agent Observability and Auditability

The harness itself needs observability. Store structured records of agent plans, tool calls, retrieval decisions, evaluation mutations, confirmations, and outcomes. This is separate from the agent-under-test trace.

Harness Session  
├── user_message  
├── plan  
├── retrieval_events\[\]  
├── tool_calls\[\]  
├── proposed_mutations\[\]  
├── confirmations\[\]  
└── final_response  
  
Agent Under Test  
├── model calls  
├── tool calls  
├── state events  
└── final output

Keeping these traces separate avoids confusing “what the evaluation harness did” with “what the evaluated agent did.”

# 41. Expanded MVP Scope for the Agent Layer

- ZIP project ingestion with deterministic file parsing.

- One canonical Python-agent adapter plus a clean adapter interface for future frameworks.

- Project knowledge graph for agents, workflows, tools, schemas, and entrypoints.

- Hybrid code/search retrieval tools.

- Conversational use-case discovery.

- Structured EvaluationSpec generation and validation.

- At least exact-match, rule/trace, schema, reference, custom-code, and optional LLM-judge evaluators.

- Evaluation run planning and Docker spin-up.

- Trace collection and evidence linking.

- Declarative dashboard renderer with a fixed component registry.

- Dashboard Skill using the component registry.

- HTML mock-data preview before execution.

- Audit logs for harness tool calls and evaluation mutations.

# 42. Recommended Build Order for the Agent Experience

1.  1\. Build deterministic project ingestion and file/symbol indexing.

2.  2\. Build the project knowledge graph and provenance model.

3.  3\. Build search tools and retrieval policies.

4.  4\. Build a read-only code-understanding agent.

5.  5\. Add workflow and evaluation use-case discovery.

6.  6\. Define the EvaluationSpec and metric/evaluator schemas.

7.  7\. Add mutating harness tools with validation and confirmation.

8.  8\. Build evaluation run planning and Docker spin-up.

9.  9\. Add trace and result retrieval to the agent.

10. 10\. Build the fixed dashboard renderer.

11. 11\. Add dashboard Skill and declarative UI generation.

12. 12\. Add plugin manifests and adapter SDK.

13. 13\. Add failure analysis, regression suggestions, and historical retrieval.

# 43. Design Decision Summary

| Area               | Decision                              | Reason                                          |
|--------------------|---------------------------------------|-------------------------------------------------|
| LLM role           | Reasoning + orchestration + authoring | Keeps platform execution deterministic          |
| Code understanding | Parser + search + LLM semantics       | Reduces hallucination and context cost          |
| Evaluation         | First-class versioned EvaluationSpec  | Provides reproducibility and governance         |
| Metrics            | User-configurable and composable      | Avoids forcing one evaluation philosophy        |
| Evaluators         | Pluggable, deterministic first        | Uses LLM judgment only when appropriate         |
| Execution          | Sandboxed workers                     | Protects control plane from arbitrary user code |
| Trace              | Canonical event model                 | Makes results explainable and inspectable       |
| UI generation      | Declarative component schema          | Prevents arbitrary frontend injection           |
| Plugins            | Manifest + permissions + SDK          | Allows extensibility without coupling           |
| Database           | PostgreSQL + pgvector                 | Simple system of record plus semantic retrieval |
| Architecture       | Modular monolith + workers first      | Fast MVP without premature microservices        |

# 44. Final Integrated Product Model

USER  
│  
▼  
CONVERSATIONAL HARNESS  
│  
┌────────────────┼────────────────┐  
│ │ │  
SEARCH REASONING TOOLS  
│ │ │  
└────────────────┼────────────────┘  
▼  
PROJECT KNOWLEDGE  
MODEL  
│  
┌───────────┴───────────┐  
▼ ▼  
USE-CASE DISCOVERY WORKFLOW MODEL  
│ │  
└───────────┬───────────┘  
▼  
EVALUATION BUILDER  
│  
▼  
EvaluationSpec  
│  
┌─────────┴─────────┐  
▼ ▼  
PREVIEW EXECUTION  
│ │  
▼ ▼  
UI / DASHBOARD DOCKER RUNNERS  
│  
▼  
TRACE COLLECTOR  
│  
▼  
METRIC ENGINE  
│  
▼  
RESULT STORE  
│  
┌───────────────┴───────────────┐  
▼ ▼  
DASHBOARDS HARNESS ANALYSIS  
│ │  
└───────────────┬───────────────┘  
▼  
ITERATION / REGRESSION

This integrated model makes the agent layer, evaluation engine, execution infrastructure, search system, and UI generation system parts of one product rather than independent features. The conversational agent is the interface to the complexity; the structured evaluation model, executor, metric engine, trace system, and component registry remain the durable technical foundation.
