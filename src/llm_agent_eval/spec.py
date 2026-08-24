"""Validated evaluation models — EvaluationSpec / TestCase / Metric (semantics §7A).

The canonical metric is one validated JSON object with seven top-level fields,
three of which are the three-layer separation this design refuses to conflate
(§7A.1): ``target`` (per-case observation), ``scoring`` (per-case
pass/fail/score), ``aggregation`` (run-level rollup), and separately ``gate``
(run-level pass condition). Conflating per-case scoring, run-level aggregation,
and the gate makes two implementers produce different numbers from one spec —
the fields are independent and explicit.

The validation invariant (STYLE invariant 2): the LLM emits validated JSON,
never code and never a string to parse — these models ARE the validation
layer; a metric that fails any validation rule is rejected with a named error,
never silently adjusted (§7A.6).

The ``on_missing`` / ``on_error`` invariant: both are required fields with NO
defaults (§7A.3/§7A.2). An unstated default silently *inverts* safety metrics:
an agent that does nothing would score 100% on "never refund ineligible
orders" under a hidden ``on_missing: pass``, and a hidden ``on_error: exclude``
lets ERROR cases vanish from headline numbers.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .events import EVENT_TYPES, Source

__all__ = [
    "TARGET_TYPES",
    "EVALUATOR_TYPES",
    "SCORING_TYPES",
    "AGGREGATION_METHODS",
    "ON_MISSING_VALUES",
    "ON_ERROR_VALUES",
    "METRIC_TYPES",
    "EXPECTED_TAGS",
    "RUN_TIERS",
    "TRACE_RULE_OPS",
    "Target",
    "Evaluator",
    "Scoring",
    "Aggregation",
    "Gate",
    "JudgeBinding",
    "Metric",
    "ExpectedValue",
    "ScriptStep",
    "TestCase",
    "EvaluationSpec",
    "SpecProblem",
    "SpecValidationError",
    "validate_spec",
]

# ---------------------------------------------------------------------------
# Closed enum values, verbatim from §7A
# ---------------------------------------------------------------------------

#: target.type — v2 §7's list, normalized (§7A.3).
TARGET_TYPES: tuple[str, ...] = (
    "input",
    "model_output",
    "tool_invocation",
    "tool_arguments",
    "tool_output",
    "state_change",
    "workflow_node",
    "final_response",
    "trace",
    "run",
    "external",
)

#: evaluator.type (§7A.2). Custom-code evaluators run in their own sandbox tier
#: and are not part of the metric schema.
EVALUATOR_TYPES: tuple[str, ...] = (
    "exact_match",
    "json_schema",
    "regex",
    "numeric",
    "reference",
    "cel_predicate",
    "trace_rule",
    "llm_judge",
    "composite",
)

#: scoring.type (§7A.2).
SCORING_TYPES: tuple[str, ...] = ("binary", "numeric", "categorical")

#: aggregation.method (§7A.2).
AGGREGATION_METHODS: tuple[str, ...] = ("pass_rate", "mean", "p50", "p95", "min", "sum")

#: target.on_missing (§7A.3) — fail | skip | pass, no default.
ON_MISSING_VALUES: tuple[str, ...] = ("fail", "skip", "pass")

#: aggregation.on_error (§7A.2) — fail | exclude, no default.
ON_ERROR_VALUES: tuple[str, ...] = ("fail", "exclude")

#: The metric-level coarse classifier (implementer brief): the harness emits
#: one of these; the evaluator carries the precise mechanism.
METRIC_TYPES: tuple[str, ...] = ("trace_rule", "scalar", "judge")

#: expected value source tags (harness §10B / §18 DDL: expected.tag).
EXPECTED_TAGS: tuple[str, ...] = ("user_stated", "inferred", "derived_from_trace")

#: run tier hints (engine §11B, plan 4I).
RUN_TIERS: tuple[str, ...] = ("quick", "standard", "full")

#: target occurrence (§7A.3): first | last | all | index:N.
_OCCURRENCE_RE = re.compile(r"^(first|last|all|index:[0-9]+)$")

#: evaluator types whose raw output is a number (binary scoring then requires
#: scoring.condition — §7A.2, never defaulted).
_NUMERIC_PRODUCING_EVALUATORS: tuple[str, ...] = ("numeric",)

#: Tool-scoped target types: `tool` names the tool (§7A.3).
_TOOL_SCOPED_TARGETS: tuple[str, ...] = ("tool_invocation", "tool_arguments", "tool_output")

# ---------------------------------------------------------------------------
# Trace-rule body validation (§9A.3) — structural, JSON-only, no evaluation
# ---------------------------------------------------------------------------

#: The closed trace-rule operator set (§9A.3).
TRACE_RULE_OPS: tuple[str, ...] = (
    "for_all",
    "exists",
    "never",
    "exists_before",
    "exists_after",
    "immediately_precedes",
    "count",
    "within_ms",
    "and",
    "or",
    "not",
    "implies",
)

#: Operators that combine child rules instead of matching events (§9A.3:
#: `match` required for all ops except and/or/not/implies).
_COMPOUND_OPS: frozenset[str] = frozenset({"and", "or"})
_UNARY_OPS: frozenset[str] = frozenset({"not", "implies"})
_MATCH_BEARING_OPS: frozenset[str] = frozenset(
    {"for_all", "exists", "never", "exists_before", "exists_after", "immediately_precedes", "count", "within_ms"}
)

#: A deliberately generous cap: the LLM emits these; a pathological nesting
#: must not blow the validation stack.
_MAX_RULE_DEPTH = 100


def validate_trace_rule_body(node: Any, *, _depth: int = 0, _root: bool = True) -> None:
    """Structurally validate a §9A.3 trace-rule node (raise ValueError).

    This validates *shape* only — op membership, required keys, match.event
    against the closed §12B event-type list, compound children — and never
    evaluates anything. ``match.where`` is carried as a CEL string and is not
    compiled here (full CEL lands with the cel-python dependency, engine
    §9A.2).
    """
    if _depth > _MAX_RULE_DEPTH:
        raise ValueError(f"trace-rule nesting exceeds {_MAX_RULE_DEPTH} levels")
    if not isinstance(node, dict):
        raise ValueError("trace-rule node must be a JSON object")
    op = node.get("op")
    if op not in TRACE_RULE_OPS:
        raise ValueError(
            f"trace-rule node requires an 'op' from the closed set {TRACE_RULE_OPS}; "
            f"got {op!r}"
        )
    if op in _COMPOUND_OPS:
        rules = node.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"trace-rule op {op!r} requires a non-empty 'rules' list")
        for child in rules:
            validate_trace_rule_body(child, _depth=_depth + 1, _root=False)
        return
    if op in _UNARY_OPS:
        if not isinstance(node.get("rule"), dict):
            raise ValueError(f"trace-rule op {op!r} requires a 'rule' child")
        validate_trace_rule_body(node["rule"], _depth=_depth + 1, _root=False)
        return
    # Match-bearing operators.
    if op == "count" and not _root:
        raise ValueError("trace-rule op 'count' is terminal and valid only as the rule's root op (§9A.3)")
    if op == "within_ms":
        if not isinstance(node.get("ms"), int):
            raise ValueError("trace-rule op 'within_ms' requires 'ms' (int milliseconds)")
    if not isinstance(node.get("match"), dict):
        raise ValueError(f"trace-rule op {op!r} requires a 'match' object")
    match = node["match"]
    event = match.get("event")
    if event not in EVENT_TYPES:
        raise ValueError(
            f"trace-rule match.event must be one of the §12B event types "
            f"{EVENT_TYPES}; got {event!r}"
        )
    tool = match.get("tool")
    if tool is not None and not isinstance(tool, str):
        raise ValueError("trace-rule match.tool must be a tool name (string)")
    where = match.get("where")
    if where is not None and not isinstance(where, str):
        raise ValueError("trace-rule match.where must be a CEL string")
    source = match.get("source")
    if source is not None and source not in tuple(s.value for s in Source):
        raise ValueError(
            f"trace-rule match.source must be proxy|adapter|runner; got {source!r}"
        )
    # Optional temporal filter on any match-bearing node (§9A.3).
    window = node.get("within_ms")
    if window is not None and not isinstance(window, int):
        raise ValueError("trace-rule 'within_ms' filter must be an int (milliseconds)")
    if op == "for_all":
        if not isinstance(node.get("assert"), dict):
            raise ValueError("trace-rule op 'for_all' requires an 'assert' rule")
        validate_trace_rule_body(node["assert"], _depth=_depth + 1, _root=False)


# ---------------------------------------------------------------------------
# §7A.3 Target — what to observe
# ---------------------------------------------------------------------------


class Target(BaseModel):
    """What to observe (§7A.3).

    ``on_missing`` is required (no default) on every target except
    ``type: run``, where §7A.5 makes it not applicable (an empty run must not
    produce a phantom pass; ``on_error`` still applies there).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "input",
        "model_output",
        "tool_invocation",
        "tool_arguments",
        "tool_output",
        "state_change",
        "workflow_node",
        "final_response",
        "trace",
        "run",
        "external",
    ]
    #: Names the tool for tool-scoped types (tool_invocation/tool_arguments/
    #: tool_output).
    tool: str | None = None
    #: JSONPath (RFC 9535) evaluated against the event payload or final
    #: response. Lightly schema-validated here (must start with '$'); the
    #: full RFC 9535 grammar check is the scoring engine's (compiled once per
    #: metric version, §7A.6 rule 5).
    selector: str | None = None
    #: first | last | all | index:N — which of the matched events feed the
    #: evaluator (§7A.3). Undeclared default when absent; the canonical
    #: example always specifies it.
    occurrence: str | None = None
    #: fail | skip | pass — required, no default (except type: run, §7A.5).
    on_missing: Literal["fail", "skip", "pass"] | None = None

    @field_validator("occurrence")
    @classmethod
    def _occurrence_shape(cls, v: str | None) -> str | None:
        if v is not None and not _OCCURRENCE_RE.match(v):
            raise ValueError(
                f"occurrence must be first | last | all | index:N, got {v!r}"
            )
        return v

    @field_validator("selector")
    @classmethod
    def _selector_shape(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("$"):
            raise ValueError(
                f"selector must be a JSONPath (RFC 9535) expression starting "
                f"with '$', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _tool_for_tool_scoped_types(self) -> "Target":
        if self.type in _TOOL_SCOPED_TARGETS and self.tool is None:
            raise ValueError(f"target.type {self.type!r} requires a tool name (§7A.3)")
        return self

    @model_validator(mode="after")
    def _on_missing_required(self) -> "Target":
        # §7A.3: on_missing has NO default — an unstated default silently
        # inverts safety metrics. §7A.5: not applicable to run targets.
        if self.type != "run" and self.on_missing is None:
            raise ValueError(
                "target.on_missing is required (fail | skip | pass) and has no default (§7A.3)"
            )
        return self


# ---------------------------------------------------------------------------
# §7A.2 Evaluator — how the observed value becomes a raw score
# ---------------------------------------------------------------------------


class Evaluator(BaseModel):
    """How the observed value becomes a raw score (§7A.2).

    Config fields are type-checked against ``type`` (§7A.6 rule 4): e.g.
    ``numeric`` requires ``expected`` and ``tolerance``; ``llm_judge`` requires
    a rubric version reference. ``trace_rule`` bodies are validated against
    the §9A.3 closed operator set; scalar checks use the §9A scalar predicate
    language, never a hand-rolled DSL and never eval().
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "exact_match",
        "json_schema",
        "regex",
        "numeric",
        "reference",
        "cel_predicate",
        "trace_rule",
        "llm_judge",
        "composite",
    ]
    #: exact_match / numeric / reference (reference: a dotted path into the
    #: test case's expected, e.g. "test.expected.refund_amount").
    expected: Any = None
    tolerance: float | None = None  # numeric
    pattern: str | None = None  # regex
    schema: dict[str, Any] | None = None  # json_schema
    #: cel_predicate — a §9A scalar predicate string. Validated as a non-empty
    #: string here; compiling it is deferred to the cel-python dependency
    #: (full CEL supports size()/matches()/contains_secret() that the starter
    #: metrics rely on — §10B.1).
    predicate: str | None = None
    #: trace_rule — a §9A.3 rule object, structurally validated (never
    #: evaluated by this layer).
    rule: dict[str, Any] | None = None
    #: llm_judge — rubric version reference (§15A).
    rubric_version_id: str | None = None

    @model_validator(mode="after")
    def _config_matches_type(self) -> "Evaluator":
        t = self.type
        if t == "exact_match" and self.expected is None:
            raise ValueError("exact_match evaluator requires 'expected'")
        if t == "json_schema" and not isinstance(self.schema, dict):
            raise ValueError("json_schema evaluator requires 'schema' (a JSON Schema)")
        if t == "regex" and not (isinstance(self.pattern, str) and self.pattern):
            raise ValueError("regex evaluator requires a non-empty 'pattern'")
        if t == "numeric":
            if not isinstance(self.expected, (int, float)) or isinstance(self.expected, bool):
                raise ValueError("numeric evaluator requires 'expected' (a number)")
            if not isinstance(self.tolerance, (int, float)):
                raise ValueError("numeric evaluator requires 'tolerance' (a number)")
        if t == "reference":
            if not isinstance(self.expected, str):
                raise ValueError(
                    "reference evaluator requires 'expected' as a field path, "
                    "e.g. 'test.expected.refund_amount'"
                )
        if t == "cel_predicate" and not (isinstance(self.predicate, str) and self.predicate):
            raise ValueError("cel_predicate evaluator requires a non-empty 'predicate'")
        if t == "trace_rule":
            if not isinstance(self.rule, dict):
                raise ValueError("trace_rule evaluator requires 'rule' (§9A.3)")
            validate_trace_rule_body(self.rule)
        if t == "llm_judge" and not (isinstance(self.rubric_version_id, str) and self.rubric_version_id):
            raise ValueError("llm_judge evaluator requires 'rubric_version_id' (§15A)")
        return self


# ---------------------------------------------------------------------------
# §7A.2 Scoring / Aggregation / Gate — per-case, per-run, run-gate
# ---------------------------------------------------------------------------


class Scoring(BaseModel):
    """Per-case result mapping (§7A.2).

    Binary scoring of a boolean evaluator passes iff the evaluator returns
    true; binary scoring of a numeric-producing evaluator requires
    ``condition`` (a §9A predicate over ``raw_value``) — never defaulted.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["binary", "numeric", "categorical"]
    #: Required for binary and numeric (§7A.6 rule 2); normalized_score is
    #: derived for weight composition.
    range: tuple[float, float] | None = None
    categories: list[str] | None = None  # categorical
    #: CEL predicate over raw_value; required (never defaulted) when binary
    #: scoring meets a numeric-producing evaluator.
    condition: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Scoring":
        if self.type in ("binary", "numeric") and self.range is None:
            raise ValueError(f"scoring.type {self.type!r} requires 'range' (§7A.6 rule 2)")
        if self.range is not None:
            lo, hi = self.range
            if lo > hi:
                raise ValueError(f"scoring.range must be [min, max], got {self.range!r}")
        if self.type == "categorical" and self.categories is None:
            raise ValueError("scoring.type 'categorical' requires 'categories'")
        return self


class Aggregation(BaseModel):
    """Per-run rollup of per-case results (§7A.2)."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["pass_rate", "mean", "p50", "p95", "min", "sum"]
    #: fail | exclude — how ERROR cases roll into the run result. REQUIRED,
    #: no default: a hidden `on_error: exclude` lets ERROR cases vanish from
    #: headline numbers (§7A.3 invariant).
    on_error: Literal["fail", "exclude"]


class Gate(BaseModel):
    """Run-level pass condition on the aggregated value (§7A.2).

    Absent ⇒ the metric reports but never fails a run. Gate semantics are
    CI-aware (§11C): for runs with repeats ≥ 3 the gate evaluates against the
    CI lower bound — that is the scoring engine's concern, not the model's.
    """

    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Gate":
        if self.min is None and self.max is None:
            raise ValueError("gate requires at least one of 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"gate.min must not exceed gate.max, got {self.min!r} / {self.max!r}")
        return self


class JudgeBinding(BaseModel):
    """The judge instrument identity (§15A.5): (provider, exact model id +
    version, schema version, rubric version). Two runs' judge metrics are
    comparable only when bindings match."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str  # exact model id + version
    schema_version: str
    rubric_version: str


# ---------------------------------------------------------------------------
# §7A Metric — the canonical object
# ---------------------------------------------------------------------------


class Metric(BaseModel):
    """One validated metric (§7A.1/§7A.2, plus metric-table identity).

    ``metric_id``/``name`` are the ``metrics`` table's identity fields (§18);
    ``type`` is the coarse classifier (trace_rule | scalar | judge). The
    seven-layer separation is explicit: ``target``, ``scoring``,
    ``aggregation`` and ``gate`` are independent fields — never conflated.
    """

    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(min_length=1)  # stable; immutable once referenced by a run
    name: str = Field(min_length=1)
    type: Literal["trace_rule", "scalar", "judge"]
    target: Target
    evaluator: Evaluator
    scoring: Scoring
    aggregation: Aggregation
    gate: Gate | None = None
    weight: float = 1.0  # composite contribution; default 1.0 (§7A.2)
    #: Required iff type == "judge" (§15A.5).
    judge_binding: JudgeBinding | None = None
    #: The UNCALIBRATED badge (§15A.1, owner decision 4J): judge scores are
    #: usable-but-provisional and excluded from gate/regression enforcement
    #: until the binding is calibrated. Defaults False — deterministic metrics
    #: are authoritative by construction; the judge validator forces True for
    #: judge metrics unless the author explicitly opted out.
    provisional: bool = False

    @model_validator(mode="after")
    def _judge_requires_binding(self) -> "Metric":
        if self.type == "judge":
            if self.judge_binding is None:
                raise ValueError(
                    "judge metric requires 'judge_binding' "
                    "(provider, model, schema_version, rubric_version — §15A.5)"
                )
            if self.evaluator.type != "llm_judge":
                raise ValueError(
                    "metric.type 'judge' requires evaluator.type 'llm_judge'"
                )
            # §15A.1: a judge metric is usable-but-provisional until its binding
            # calibrates. The author may explicitly opt out; otherwise the
            # shipped default holds.
            if "provisional" not in self.model_fields_set:
                self.provisional = True
        return self

    @model_validator(mode="after")
    def _scoring_condition_required(self) -> "Metric":
        # §7A.2: binary scoring of a numeric-producing evaluator requires
        # scoring.condition (a predicate over raw_value) — never defaulted.
        numeric_producing = self.evaluator.type in _NUMERIC_PRODUCING_EVALUATORS or (
            self.evaluator.type == "trace_rule"
            and isinstance(self.evaluator.rule, dict)
            and self.evaluator.rule.get("op") == "count"
        )
        if self.scoring.type == "binary" and numeric_producing and not self.scoring.condition:
            raise ValueError(
                "binary scoring of a numeric-producing evaluator requires "
                "scoring.condition (a §9A predicate over raw_value) — never defaulted"
            )
        return self


# ---------------------------------------------------------------------------
# TestCase (§7A/brief) — identity, input, expected-with-tags, U2 script
# ---------------------------------------------------------------------------


class ExpectedValue(BaseModel):
    """One expected value with its source tag (harness §10B, §18 DDL:
    expected.tag ∈ user_stated | inferred | derived_from_trace).

    ``user_stated`` is the default: inferred/derived values are the ones that
    must be surfaced in review and never silently authoritative (§38 step 8).
    """

    model_config = ConfigDict(extra="forbid")

    tag: Literal["user_stated", "inferred", "derived_from_trace"] = "user_stated"
    value: Any


class ScriptStep(BaseModel):
    """One interactive script step (U2, §10B): the agent waits for a human
    (or the user-simulator) and receives a response. Payload shapes mirror the
    §12B.3 wait_for_input / user_response event schemas."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["wait_for_input", "user_response"]
    requested_input: str | None = None  # wait_for_input: what the agent asked for
    provided_input: str | None = None  # user_response: the supplied answer
    #: user_response: simulator (U1, default) | real_user (manual HITL).
    source: Literal["simulator", "real_user"] = "simulator"

    @model_validator(mode="after")
    def _shape(self) -> "ScriptStep":
        if self.kind == "wait_for_input" and self.provided_input is not None:
            raise ValueError(
                "wait_for_input steps carry 'requested_input', not 'provided_input'"
            )
        if self.kind == "user_response" and self.requested_input is not None:
            raise ValueError(
                "user_response steps carry 'provided_input', not 'requested_input'"
            )
        return self


class TestCase(BaseModel):
    """A test case (§7A/§18 test_cases). ``case_id`` is required — a case
    without an id is rejected. Expected values are tagged with a source
    (user_stated | inferred | derived_from_trace)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)  # a case without an id is rejected
    name: str = Field(min_length=1)
    description: str = ""
    input: dict[str, Any]
    expected: dict[str, ExpectedValue] = Field(default_factory=dict)
    #: Case metadata (tags, category, difficulty — consumed by §9A.2's `case`
    #: variable and §10A.5 fault injection's on_case_tag); carried as a free
    #: JSON object, matching §18's test_cases.metadata.
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Optional interactive script (U2): wait_for_input / user_response steps.
    script: list[ScriptStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# EvaluationSpec
# ---------------------------------------------------------------------------


class EvaluationSpec(BaseModel):
    """The frozen evaluation object (§7A, engine §11A/§18
    evaluation_versions.spec): cases + metrics, with optional run-tier hints.
    The harness generates it as validated JSON; the harness's repair loop
    consumes ``SpecValidationError.problems`` (see validate_spec)."""

    model_config = ConfigDict(extra="forbid")

    spec_version: str
    name: str
    dataset_version: str
    cases: list[TestCase]
    metrics: list[Metric]
    #: Optional run-tier hint (engine §11B: quick | standard | full).
    run_tier: Literal["quick", "standard", "full"] | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> "EvaluationSpec":
        dup_cases = [c for c, n in Counter(c.case_id for c in self.cases).items() if n > 1]
        if dup_cases:
            raise ValueError(f"duplicate case_id(s): {', '.join(sorted(dup_cases))}")
        dup_metrics = [m for m, n in Counter(m.metric_id for m in self.metrics).items() if n > 1]
        if dup_metrics:
            raise ValueError(f"duplicate metric_id(s): {', '.join(sorted(dup_metrics))}")
        return self


# ---------------------------------------------------------------------------
# Validation entry point — the harness's repair loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecProblem:
    """One structured validation problem: a dotted field path plus a message.
    The harness's repair loop consumes these to fix LLM-emitted specs."""

    field: str
    message: str


class SpecValidationError(ValueError):
    """Raised by validate_spec; carries a structured list of SpecProblem."""

    def __init__(self, problems: list[SpecProblem]):
        self.problems = problems
        detail = "; ".join(f"{p.field}: {p.message}" for p in problems)
        super().__init__(
            f"EvaluationSpec validation failed with {len(problems)} problem(s): {detail}"
        )


def _loc_to_string(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            if not parts:
                parts.append(f"[{part}]")
            else:
                parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    if not parts:
        return "spec"
    return ".".join(parts)


def validate_spec(spec_dict: dict[str, Any] | str) -> EvaluationSpec:
    """Validate a spec-shaped dict (or JSON text) into an EvaluationSpec.

    On failure raises :class:`SpecValidationError` whose ``problems`` is a
    structured list of ``(field, message)`` — the harness's repair loop
    consumes it. Rejected, never silently adjusted (§7A.6).
    """
    try:
        if isinstance(spec_dict, str):
            return EvaluationSpec.model_validate_json(spec_dict)
        return EvaluationSpec.model_validate(spec_dict)
    except ValidationError as exc:
        problems = [
            SpecProblem(field=_loc_to_string(err["loc"]), message=err["msg"])
            for err in exc.errors()
        ]
        raise SpecValidationError(problems) from exc
