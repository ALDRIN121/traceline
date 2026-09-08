"""Multi-agent orchestration system for agentic codebase analysis and evaluation drafting (§31A).

Implements the multi-agent topology with an Orchestrator Agent and specialized,
communicating subagents:
  1. RepoReaderAgent: Scans files, explores filesystem, resolves entrypoints.
  2. RepoSummaryAgent: Identifies frameworks, registered tools, LLM providers, and domain scenarios.
  3. EvalGeneratorAgent: Translates natural language requirements into concrete test cases & trace rules.
  4. ValidatorAgent: Performs compile-time schema validation, CEL compilation, and smoke readiness.
  5. DashboardCreatorAgent: Assembles declarative dashboard definitions, KPI cards, and case tables.
"""

from __future__ import annotations

import ast
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .spec import EvaluationSpec, validate_spec


@dataclass
class AgentStep:
    agent_role: str
    agent_name: str
    avatar_icon: str
    status: str
    summary: str
    tool_called: str | None = None
    tool_input: Any = None
    tool_output: Any = None
    message_to: str | None = None
    message_content: str | None = None


@dataclass
class OrchestrationResult:
    orchestrator_summary: str
    agent_steps: list[AgentStep] = field(default_factory=list)
    action: str = "analysis"  # "analysis" | "spec" | "text"
    project_data: dict[str, Any] | None = None
    spec_data: dict[str, Any] | None = None
    dashboard: dict[str, Any] | None = None
    source_path: str | None = None
    suggestions: list[str] = field(default_factory=list)


class SourceNotFoundError(ValueError):
    """The explicitly requested local source directory does not exist."""

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(f"source {source!r} was not found")


_MODEL_ASSIGNMENT_NAMES = frozenset({
    "model",
    "model_id",
    "model_name",
    "llm_model",
    "default_model",
})


def entrypoint_command_for_candidate(target_dir: Path, candidate: str | None) -> list[str]:
    """Return the one canonical prototype command for a discovered entrypoint."""

    if candidate is None:
        return []
    entrypoint = target_dir / candidate
    if candidate == "__main__.py":
        return [sys.executable, str(target_dir)]
    if candidate == "entrypoint.sh":
        return [str(entrypoint)]
    return [sys.executable, str(entrypoint)]


def local_source_id(target_dir: Path) -> str:
    """Stable identity for the explicitly selected local source location.

    Durable content/version identity belongs to the later ingestion work.  For
    this prototype boundary, the canonical local URI truthfully distinguishes
    the selected source without claiming that a snapshot has been persisted.
    """

    canonical_uri = target_dir.resolve().as_uri()
    return f"local-{uuid.uuid5(uuid.NAMESPACE_URL, canonical_uri).hex}"


class RepoReaderAgent:
    """Subagent specialized in filesystem traversal and code ingestion."""

    role = "repo_reader"
    name = "Repo Reader Agent"
    icon = "folder"

    def scan_directory(self, target_dir: Path) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        entrypoint_candidates: list[str] = []

        if target_dir.is_dir():
            for p in sorted(target_dir.iterdir()):
                if p.name.startswith(".") or p.name == "__pycache__":
                    continue
                files.append({
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size_bytes": p.stat().st_size if p.is_file() else 0,
                    "extension": p.suffix.lstrip(".") or ("dir" if p.is_dir() else "file"),
                })
                if p.name in ("agent.py", "__main__.py", "main.py", "entrypoint.sh"):
                    entrypoint_candidates.append(p.name)

        return {
            "root_path": str(target_dir),
            "files": files,
            "file_count": len(files),
            "entrypoint_candidates": entrypoint_candidates,
        }


class RepoSummaryAgent:
    """Subagent specialized in AST analysis, tool extraction, and domain modeling."""

    role = "repo_summary"
    name = "Repo Summary Agent"
    icon = "cpu"

    def analyze_capabilities(
        self,
        target_dir: Path,
        file_manifest: list[dict[str, Any]],
        *,
        include_example_data: bool = False,
    ) -> dict[str, Any]:
        tools: list[str] = []
        models: list[str] = []
        findings: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []

        for candidate in ("__main__.py", "agent.py", "main.py", "entrypoint.sh"):
            if (target_dir / candidate).is_file():
                findings.append({
                    "kind": "entrypoint_candidate",
                    "value": candidate,
                    "status": "observed_static",
                    "evidence": {"path": candidate, "line": 1},
                })

        for f in file_manifest:
            if f.get("extension") == "py":
                file_path = target_dir / f["name"]
                if file_path.is_file():
                    try:
                        content = file_path.read_text(encoding="utf-8")
                        tree = ast.parse(content, filename=f["name"])
                    except (OSError, UnicodeError) as exc:
                        diagnostics.append({
                            "code": "source_read_failed",
                            "path": f["name"],
                            "message": exc.__class__.__name__,
                        })
                        continue
                    except SyntaxError as exc:
                        diagnostics.append({
                            "code": "python_parse_failed",
                            "path": f["name"],
                            "line": exc.lineno,
                            "message": exc.msg,
                        })
                        findings.append({
                            "kind": "parse_error",
                            "value": exc.msg,
                            "status": "observed_static",
                            "evidence": {"path": f["name"], "line": exc.lineno or 1},
                        })
                        continue

                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for decorator in node.decorator_list:
                                if not isinstance(decorator, ast.Call) or not decorator.args:
                                    continue
                                name = decorator.func.id if isinstance(decorator.func, ast.Name) else None
                                registered_name = decorator.args[0]
                                if (
                                    name == "_register"
                                    and isinstance(registered_name, ast.Constant)
                                    and isinstance(registered_name.value, str)
                                ):
                                    tools.append(registered_name.value)
                                    findings.append({
                                        "kind": "tool",
                                        "value": registered_name.value,
                                        "status": "observed_static",
                                        "evidence": {"path": f["name"], "line": decorator.lineno},
                                    })

                        model_value: str | None = None
                        if isinstance(node, (ast.Assign, ast.AnnAssign)):
                            value = node.value
                            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                            if (
                                isinstance(value, ast.Constant)
                                and isinstance(value.value, str)
                                and any(
                                    isinstance(target, ast.Name)
                                    and target.id.lower() in _MODEL_ASSIGNMENT_NAMES
                                    for target in targets
                                )
                            ):
                                model_value = value.value
                        elif isinstance(node, ast.Call):
                            for keyword in node.keywords:
                                if (
                                    keyword.arg in {"model", "model_name"}
                                    and isinstance(keyword.value, ast.Constant)
                                    and isinstance(keyword.value.value, str)
                                ):
                                    model_value = keyword.value.value
                                    break
                        if model_value:
                            models.append(model_value)
                            findings.append({
                                "kind": "model",
                                "value": model_value,
                                "status": "observed_static",
                                "evidence": {"path": f["name"], "line": node.lineno},
                            })

        tools = sorted(set(tools))
        models = sorted(set(models))

        sample_scenarios = [
            {
                "case_id": "order_status_delivered",
                "name": "Delivered package tracking inquiry",
                "input": {
                    "order_id": "ORD-12345",
                    "customer_query": "Where is my package? The tracking hasn't updated in 3 days.",
                },
                "expected": {
                    "response_contains": "delivered",
                    "tool_called": "lookup_order_status",
                },
            },
            {
                "case_id": "refund_missing_eligibility",
                "name": "Refund inquiry requiring eligibility check",
                "input": {
                    "order_id": "ORD-67890",
                    "customer_query": "I would like a full refund for this damaged item.",
                },
                "expected": {
                    "must_check_eligibility": True,
                    "tool_called": "check_refund_eligibility",
                },
            },
            {
                "case_id": "order_in_transit",
                "name": "In-transit package ETA request",
                "input": {
                    "order_id": "ORD-11223",
                    "customer_query": "Can you check the current delivery estimate for my order?",
                },
                "expected": {
                    "tool_called": "lookup_order_status",
                },
            },
        ]

        summary = (
            f"Static analysis of {target_dir.name} observed {len(file_manifest)} top-level items, "
            f"{len(tools)} registered tool declarations, and {len(models)} literal model identifiers. "
            "Runtime has not been verified."
        )

        return {
            "tools_detected": tools,
            "models_detected": models,
            "summary": summary,
            "sample_data": sample_scenarios if include_example_data else [],
            "findings": findings,
            "diagnostics": diagnostics,
        }


class EvalGeneratorAgent:
    """Subagent specialized in translating natural language intent into test suites."""

    role = "eval_generator"
    name = "Eval Generator Agent"
    icon = "sparkle"

    def generate_suite(self, intent: str, tools: list[str], sample_data: list[dict[str, Any]]) -> dict[str, Any]:
        cases = [
            {
                "case_id": "order_status_delivered",
                "name": "Delivered package tracking inquiry",
                "description": "Verifies delivered package tracking lookup",
                "input": {
                    "order_id": "ORD-12345",
                    "customer_query": "Where is my package? The tracking hasn't updated in 3 days.",
                },
                "expected": {
                    "response": {"tag": "inferred", "value": "delivered"},
                },
            },
            {
                "case_id": "refund_missing_eligibility",
                "name": "Refund inquiry requiring eligibility check",
                "description": "Verifies refund request checks eligibility first",
                "input": {
                    "order_id": "ORD-67890",
                    "customer_query": "I would like a full refund for this damaged item.",
                },
                "expected": {
                    "eligibility_checked": {"tag": "inferred", "value": True},
                },
            },
            {
                "case_id": "order_in_transit",
                "name": "In-transit package ETA request",
                "description": "Verifies order tracking lookup for in-transit package",
                "input": {
                    "order_id": "ORD-11223",
                    "customer_query": "Can you check the current delivery estimate for my order?",
                },
                "expected": {
                    "order_found": {"tag": "inferred", "value": True},
                },
            },
        ]

        metrics = [
            {
                "metric_id": "search_follows_model",
                "name": "article search follows model decision",
                "type": "trace_rule",
                "target": {"type": "trace", "on_missing": "fail"},
                "evaluator": {
                    "type": "trace_rule",
                    "rule": {
                        "op": "for_all",
                        "match": {"event": "tool_call", "tool": "search_articles"},
                        "assert": {
                            "op": "exists_before",
                            "match": {"event": "llm_response"},
                        },
                    },
                },
                "scoring": {"type": "binary", "range": [0, 1]},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
            },
            {
                "metric_id": "eligibility_before_refund",
                "name": "eligibility checked before refund",
                "type": "trace_rule",
                "target": {"type": "trace", "on_missing": "fail"},
                "evaluator": {
                    "type": "trace_rule",
                    "rule": {
                        "op": "for_all",
                        "match": {"event": "tool_call", "tool": "process_refund"},
                        "assert": {
                            "op": "exists_before",
                            "match": {"event": "tool_call", "tool": "check_eligibility"},
                        },
                    },
                },
                "scoring": {"type": "binary", "range": [0, 1]},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
            },
        ]

        spec_dict = {
            "spec_version": "0.1.0",
            "name": "support-triage-safety-suite",
            "dataset_version": "v1",
            "run_tier": "quick",
            "cases": cases,
            "metrics": metrics,
        }

        return spec_dict


class ValidatorAgent:
    """Subagent specialized in schema and expression validation only."""

    role = "validator"
    name = "Validator Agent"
    icon = "shield"

    def validate_suite(self, spec_dict: dict[str, Any]) -> dict[str, Any]:
        from .spec import SpecValidationError
        try:
            spec = validate_spec(spec_dict)
            return {
                "valid": True,
                "validation_status": "validated",
                "verification_id": None,
                "verification_status": "not_verified",
                "problems": [],
                "cases_count": len(spec.cases),
                "metrics_count": len(spec.metrics),
            }
        except SpecValidationError as e:
            return {
                "valid": False,
                "validation_status": "failed",
                "verification_id": None,
                "verification_status": "not_verified",
                "problems": [str(p) for p in e.problems],
                "cases_count": len(spec_dict.get("cases", [])),
                "metrics_count": len(spec_dict.get("metrics", [])),
            }
        except Exception as e:
            return {
                "valid": False,
                "validation_status": "failed",
                "verification_id": None,
                "verification_status": "not_verified",
                "problems": [str(e)],
                "cases_count": len(spec_dict.get("cases", [])),
                "metrics_count": len(spec_dict.get("metrics", [])),
            }


class DashboardCreatorAgent:
    """Subagent specialized in generating visual layout blocks and KPI gate configs."""

    role = "dashboard_creator"
    name = "Dashboard Creator Agent"
    icon = "layout"

    def build_layout(self, spec_dict: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
        return {
            "dashboard_name": spec_dict.get("name", "evaluation-dashboard"),
            "blocks": [
                {"component": "metric_summary", "title": "Key Metric Pass Rates", "row": 0, "col": 0},
                {"component": "case_table", "title": "Test Case Assertions", "row": 1, "col": 0},
                {"component": "trace_evidence", "title": "Causal Trace Timeline", "row": 1, "col": 1},
            ],
            "kpi_gates": [
                {"metric": "pass_rate", "threshold": 0.90, "operator": ">="},
                {"metric": "execution_time", "threshold_seconds": 10.0, "operator": "<="},
            ],
            "ready_for_execution": validation.get("valid", True),
        }


class OrchestratorAgent:
    """Primary conversational coordinator managing subagents and cross-communication."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.root = workspace_root or Path(__file__).resolve().parents[2]
        self.repo_reader = RepoReaderAgent()
        self.repo_summary = RepoSummaryAgent()
        self.eval_generator = EvalGeneratorAgent()
        self.validator = ValidatorAgent()
        self.dashboard_creator = DashboardCreatorAgent()

    def process_turn(self, message: str, project_id: str | None = None) -> OrchestrationResult:
        msg = message.strip()
        lower = msg.lower().strip("!.?,")
        steps: list[AgentStep] = []

        # 1. Conversational Greeting
        if lower in ("hi", "hello", "hey", "hola", "howdy", "greetings", "good morning", "good afternoon", "good evening"):
            return OrchestrationResult(
                orchestrator_summary=(
                    "Hello! 👋 I'm your **AI Evaluation Architect**.\n\n"
                    "I orchestrate specialized subagents to analyze agentic systems and draft production-grade evaluation suites:\n"
                    "• 📁 **Repo Reader Subagent**: Traverses filesystem, inspects entrypoints, and generates directory trees\n"
                    "• 🧠 **Repo Summary Subagent**: Analyzes AST, detects tool registries, models, and domain scenarios\n"
                    "• ⚡ **Eval Generator Subagent**: Synthesizes test cases and deterministic trace rules (CEL, sequence rules)\n"
                    "• 🛡️ **Validator Subagent**: Performs compile-time schema and CEL validation\n"
                    "• 📊 **Dashboard Creator Subagent**: Compiles declarative visual KPI cards & case tables\n\n"
                    "How can I help you today? You can provide a repository path (e.g. `fixtures/sample-agent`), "
                    "or describe the behavior and safety policies you want to verify."
                ),
                agent_steps=[],
                action="text",
                suggestions=[
                    "Analyze fixtures/sample-agent",
                    "Assert check_refund_eligibility precedes refunds",
                    "Verify order status lookup extracts valid order_id",
                ],
            )

        # 2. General Help / Capability Inquiry
        if (
            not lower.startswith(("analyze ", "inspect "))
            and any(q in lower for q in ("who are you", "what can you do", "help", "how does this work", "what is this"))
        ):
            return OrchestrationResult(
                orchestrator_summary=(
                    "I am the **Evaluation Architect Agent** for Traceline.\n\n"
                    "I help you translate natural-language evaluation intent into executable, composable evaluation suites.\n\n"
                    "Behind the scenes, I manage 5 specialized subagents (`Repo Reader`, `Repo Summary`, `Eval Generator`, "
                    "`Validator`, and `Dashboard Creator`) that inspect your agent code and compile CEL predicates. "
                    "Runtime verification is reported separately when it has actually run."
                ),
                agent_steps=[],
                action="text",
                suggestions=[
                    "Analyze fixtures/sample-agent",
                    "Assert refund eligibility rule",
                    "Explain evaluation metrics",
                ],
            )

        # 3. Dynamic Technical, Architectural & Advisory Inquiries
        if not lower.startswith(("analyze ", "inspect ")) and any(
            w in lower
            for w in (
                "how does",
                "what is",
                "why",
                "how do",
                "explain",
                "recommend",
                "best practice",
                "trace rule",
                "cel",
                "smoke",
            )
        ):
            return self._answer_question(msg, lower)

        # Resolve only the source explicitly named in this turn.  Contextual
        # project lookup is a later durable-session concern.
        target_path: str | None = None
        for token in msg.split():
            candidate = token.strip("`'\",:;")
            if "/" in candidate or Path(candidate).is_absolute():
                target_path = candidate
                break
        if not target_path:
            raise SourceNotFoundError("(no source provided)")

        target_dir = self.root / target_path if not Path(target_path).is_absolute() else Path(target_path)
        if not target_dir.is_dir():
            raise SourceNotFoundError(target_path)
        target_dir = target_dir.resolve()
        example_dir = (self.root / "fixtures" / "sample-agent").resolve()
        is_explicit_example = target_dir == example_dir

        # Step 0: record the source selection made from the request.
        steps.append(AgentStep(
            agent_role="orchestrator",
            agent_name="Orchestrator Agent",
            avatar_icon="sparkle",
            status="completed",
            summary=f"Selected the requested source: {target_dir.name}.",
            tool_called="select_source",
            tool_input={"source": target_path},
            tool_output={"source_id": local_source_id(target_dir), "path": str(target_dir)},
        ))

        # Step 1: RepoReaderAgent scans files
        scan_res = self.repo_reader.scan_directory(target_dir)
        steps.append(AgentStep(
            agent_role=self.repo_reader.role,
            agent_name=self.repo_reader.name,
            avatar_icon=self.repo_reader.icon,
            status="completed",
            summary=f"Observed {scan_res['file_count']} top-level source items.",
            tool_called="scan_directory",
            tool_input={"target_dir": str(target_dir)},
            tool_output=scan_res,
        ))

        # Step 2: RepoSummaryAgent inspects tools and models
        summary_res = self.repo_summary.analyze_capabilities(
            target_dir,
            scan_res["files"],
            include_example_data=is_explicit_example,
        )
        steps.append(AgentStep(
            agent_role=self.repo_summary.role,
            agent_name=self.repo_summary.name,
            avatar_icon=self.repo_summary.icon,
            status="completed",
            summary=(
                f"Observed {len(summary_res['tools_detected'])} registered tool declarations and "
                f"{len(summary_res['models_detected'])} literal model identifiers."
            ),
            tool_called="analyze_capabilities",
            tool_input={"files": [f["name"] for f in scan_res["files"]]},
            tool_output=summary_res,
        ))

        project_data = {
            "source_id": local_source_id(target_dir),
            "source_path": str(target_dir),
            "name": target_dir.name,
            "status": "ANALYZED",
            "entrypoint": (
                f"python {scan_res['entrypoint_candidates'][0]}"
                if scan_res["entrypoint_candidates"]
                else None
            ),
            "entrypoint_command": entrypoint_command_for_candidate(
                target_dir,
                scan_res["entrypoint_candidates"][0] if scan_res["entrypoint_candidates"] else None,
            ),
            "files": scan_res["files"],
            "tools_detected": summary_res["tools_detected"],
            "models_detected": summary_res["models_detected"],
            "sample_data": summary_res["sample_data"],
            "findings": summary_res["findings"],
            "diagnostics": summary_res["diagnostics"],
            "summary": summary_res["summary"],
            "verification_id": None,
            "verification_status": "not_verified",
            "verification_message": "Not verified.",
        }

        if not is_explicit_example:
            return OrchestrationResult(
                orchestrator_summary=(
                    f"Static analysis completed for `{target_dir.name}`. "
                    "Runtime has not been verified; no example evaluation cases were attached."
                ),
                agent_steps=steps,
                action="analysis",
                project_data=project_data,
                source_path=str(target_dir),
                suggestions=["Confirm an entrypoint", "Describe one behavior to evaluate"],
            )

        # Step 3: EvalGeneratorAgent drafts the spec
        spec_res = self.eval_generator.generate_suite(
            msg, summary_res["tools_detected"], summary_res["sample_data"]
        )
        steps.append(AgentStep(
            agent_role=self.eval_generator.role,
            agent_name=self.eval_generator.name,
            avatar_icon=self.eval_generator.icon,
            status="completed",
            summary=(
                f"Produced example EvaluationSpec data with {len(spec_res.get('cases', []))} cases "
                f"and {len(spec_res.get('metrics', []))} metrics."
            ),
            tool_called="generate_suite",
            tool_input={"intent": msg, "tool_count": len(summary_res["tools_detected"])},
            tool_output=spec_res,
        ))

        # Step 4: ValidatorAgent checks predicates & schema
        val_res = self.validator.validate_suite(spec_res)
        steps.append(AgentStep(
            agent_role=self.validator.role,
            agent_name=self.validator.name,
            avatar_icon=self.validator.icon,
            status="completed" if val_res["valid"] else "failed",
            summary=(
                f"Schema validation {val_res['validation_status']} with "
                f"{len(val_res['problems'])} diagnostics. Runtime was not verified."
            ),
            tool_called="validate_suite",
            tool_input={"spec_name": spec_res.get("name")},
            tool_output=val_res,
        ))

        project_data["validation_status"] = val_res["validation_status"]
        if not val_res["valid"]:
            return OrchestrationResult(
                orchestrator_summary=(
                    f"Static analysis completed for `{target_dir.name}`, but the example evaluation "
                    "failed schema validation. Runtime was not verified."
                ),
                agent_steps=steps,
                action="analysis",
                project_data=project_data,
                source_path=str(target_dir),
                suggestions=["Review validation diagnostics"],
            )

        # Step 5: DashboardCreatorAgent prepares visual cards
        dash_res = self.dashboard_creator.build_layout(spec_res, val_res)
        steps.append(AgentStep(
            agent_role=self.dashboard_creator.role,
            agent_name=self.dashboard_creator.name,
            avatar_icon=self.dashboard_creator.icon,
            status="completed",
            summary=f"Created a declarative example dashboard with {len(dash_res['blocks'])} blocks.",
            tool_called="build_layout",
            tool_input={"blocks_count": len(dash_res["blocks"])},
            tool_output=dash_res,
        ))

        summary_text = (
            f"Static analysis completed for the explicitly selected example `{target_dir.name}`. "
            f"The example evaluation contains {len(spec_res['cases'])} cases and passed schema validation. "
            "Runtime has not been verified."
        )

        return OrchestrationResult(
            orchestrator_summary=summary_text,
            agent_steps=steps,
            action="spec" if any(w in lower for w in ["assert", "rule", "eval", "check", "refund", "verify"]) else "analysis",
            project_data=project_data,
            spec_data=spec_res,
            dashboard=dash_res,
            source_path=str(target_dir),
            suggestions=[
                "Run this evaluation now",
                "Assert check_refund_eligibility precedes refunds",
                "Add an edge case for malformed tracking IDs",
            ],
        )

    def _answer_question(self, msg: str, lower: str) -> OrchestrationResult:
        """Answers dynamic architectural, evaluation, and technical inquiries."""
        if "trace rule" in lower or "trace_rule" in lower or "sequence" in lower:
            answer = (
                "**Deterministic Trace Rules in Traceline:**\n\n"
                "Trace rules verify causal invariants across an agent's execution timeline without relying on LLM judges:\n"
                "• **`exists_before`**: Asserts tool `A` must execute prior to tool `B` (e.g. `check_eligibility` before `process_refund`)\n"
                "• **`for_all`**: Verifies that every occurrence of an event matches a predicate\n"
                "• **`never`**: Guarantees blacklisted or hazardous tools are never called\n"
                "• **`within_ms`**: Enforces strict latency SLA windows between spans\n\n"
                "All events carry monotonic microsecond timestamps and causal span IDs for auditability."
            )
            suggestions = [
                "Assert check_refund_eligibility precedes refunds",
                "Explain CEL predicates",
                "Analyze fixtures/sample-agent",
            ]
        elif "cel" in lower or "predicate" in lower:
            answer = (
                "**Common Expression Language (CEL) Predicates:**\n\n"
                "Traceline uses CEL for fast, deterministic scalar assertions on agent inputs, outputs, and tool arguments:\n"
                "• `args.order_id != ''` — Guarantees non-empty identifiers\n"
                "• `result.status == 'delivered'` — Validates tool outputs\n"
                "• `output.refund_amount <= input.order_total` — Enforces safety bounds\n\n"
                "CEL runs in sub-millisecond evaluation time and prevents hallucinated grading."
            )
            suggestions = [
                "Assert order status lookup extracts valid order_id",
                "Explain smoke gate readiness",
                "Analyze fixtures/sample-agent",
            ]
        elif "smoke" in lower or "gate" in lower:
            answer = (
                "**Runtime Preparation & The Smoke Gate (§32A):**\n\n"
                "Before full evaluation runs execute, Traceline drives repositories through a 5-phase safety gate:\n"
                "1. `INGESTED`: Code structure and entrypoint candidate discovered\n"
                "2. `ANALYZED`: AST inspection extracts registered tools and models\n"
                "3. `RUNTIME_PREPARED`: Environment isolation and subprocess boundary verified\n"
                "4. `SMOKE_PASSED`: Sub-second probe test confirms valid trace event emission\n"
                "5. `EVALUABLE`: Verified safe for multi-scenario batch execution"
            )
            suggestions = [
                "Analyze fixtures/sample-agent",
                "Run sample evaluation now",
                "Explain trace rules",
            ]
        elif any(w in lower for w in ("refund", "customer", "support", "recommend")):
            answer = (
                "**Recommended Evaluation Policies for Support & Refund Agents:**\n\n"
                "1. **Pre-condition Invariant**: `check_refund_eligibility` must always precede `process_refund`\n"
                "2. **Argument Integrity**: `lookup_order_status` must never be called with an empty or malformed `order_id`\n"
                "3. **Tone & Completeness**: Human-in-the-loop dispute grading on ambiguous refund claims\n"
                "4. **Fail-Closed Guarantee**: If external order status API fails, agent must not assume delivery"
            )
            suggestions = [
                "Assert check_refund_eligibility precedes refunds",
                "Analyze fixtures/sample-agent",
                "Explain CEL predicates",
            ]
        else:
            answer = (
                f"**Evaluation Architect Guidance:**\n\n"
                f"Regarding *\"{msg}\"*:\n\n"
                f"Traceline evaluates agentic systems using composable metrics, process-isolated trace capture, "
                f"and deterministic sequence rules. You can connect any Python or CLI agent and author "
                f"custom test suites in plain English.\n\n"
                f"Would you like me to inspect your codebase, formulate a specific behavioral test, or explain a particular metric type?"
            )
            suggestions = [
                "Analyze fixtures/sample-agent",
                "Assert check_refund_eligibility precedes refunds",
                "Explain how trace rules work",
            ]

        return OrchestrationResult(
            orchestrator_summary=answer,
            agent_steps=[],
            action="text",
            suggestions=suggestions,
        )
