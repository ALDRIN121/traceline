"""Per-case scoring and run-level aggregation (semantics §7A, §9A, §11C, §13A).

The three-layer separation, never conflated (§7A.1):

- **Per-case scoring** — :func:`evaluate_case` / :func:`score_attempt` turn one
  case's trace events + one metric into a :class:`CaseScore`.
- **Run-level aggregation** — :func:`aggregate_metric` rolls per-case results
  into a :class:`RunAggregate` with ``aggregation_state`` IN_PROGRESS /
  PARTIAL / COMPLETE (§13A.3). PARTIAL when some cases are missing — the UI
  shows partial, never fakes complete.
- **The gate** — :func:`evaluate_gate` applies the metric's run-level gate to
  the aggregated value; :func:`gate_safe` excludes provisional metrics (judge
  metrics whose binding is not CALIBRATED, §15A.1) from gate enforcement.

First attempts are authoritative (§11C.3): :func:`evaluate_case` scores only
attempt-0 events — a case that passes on retry 2 can never re-enter the pass
rate. :func:`score_attempt` flags retry scores ``on_retry_override`` and
:func:`aggregate_metric` drops them defensively (the §18.6 back-door guard).

``on_missing`` has NO default (§7A.3, enforced by spec.py) and decides the
case score when:

- **trace-rule metrics**: the attempt trace is fully empty — §9A.5: "the
  metric-level ``on_missing`` still governs a fully empty trace (no events at
  all)". A non-empty trace is scored by the rule itself: ``never`` with zero
  matches is a PASS, ``for_all`` with zero outer matches is vacuously true,
  ``exists`` with zero matches fails — the §9A.5 G6 fix means an empty trace
  is the only case on_missing governs.
- **scalar / judge metrics**: the target matched zero events (§7A.3).

``on_error`` has NO default (aggregation.on_error, §7A.2/§7A.4) and decides
how ERROR case results roll into the run result: ``fail`` counts them as
failures (denominator includes them, score contribution 0.0); ``exclude``
drops them from the denominator and reports ``error_n`` separately. Either
way the per-case ERROR rows are stored; nothing is silently discarded.
SKIPPED results (on_missing: skip) are excluded from the denominator and
reported as ``skipped_n``.

This module consumes **agent-under-test traces only** — harness sessions are a
different surface and are never mixed in (invariant: two trace streams never
conflated). Everything here is deterministic: no network, no randomness, no
sampling parameters anywhere.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from .events import EventType, TraceEvent
from .judge import (
    JudgeEvaluator,
    JudgeReadinessRegistry,
    JudgmentError,
    ReadinessState,
)
from .predicates import MISSING, Predicate, PredicateError, UNKNOWN
from .retrieval import citation_correctness, recall_at_k
from .spec import EvaluationSpec, Metric, TestCase
from .trace_rules import TraceRuleError, evaluate_rule

__all__ = [
    "CaseStatus",
    "AggregationState",
    "CaseScore",
    "RunAggregate",
    "GateResult",
    "EvaluationError",
    "ConditionError",
    "UnsupportedSelectorError",
    "evaluate_case",
    "score_attempt",
    "aggregate_metric",
    "evaluate_gate",
    "gate_safe",
]


class CaseStatus(str, Enum):
    """Per-case result statuses (§7A.4): PASS / FAIL / ERROR / SKIPPED. ERROR
    is an evaluator or infrastructure failure — never an assertion failure.
    SKIPPED comes only from on_missing: skip."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class AggregationState(str, Enum):
    """§13A.3 aggregation_state on the metric row — separate from runs.status.
    IN_PROGRESS: cases still executing. PARTIAL: the run is done executing but
    some aggregation input is missing or failed. COMPLETE: final for this
    score_revision."""

    IN_PROGRESS = "IN_PROGRESS"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class CaseScore:
    """One metric's per-case result. ``status`` is the authoritative §7A.4
    status; ``passed`` is derived from it (False for ERROR/SKIPPED); ``score``
    is the per-case score value (1.0/0.0 for binary, the normalized value for
    numeric, None for ERROR/SKIPPED). ``evidence_event_ids`` are the trace
    event ids the result links to (§12B.7/§9A.7.3). ``attempt``/``on_retry_override``
    record first-attempt authority (§11C.3); ``provisional`` marks judge-derived
    scores that must not gate (§15A.1)."""

    metric_id: str
    case_id: str
    score: float | None
    passed: bool
    evidence_event_ids: tuple[str, ...]
    status: CaseStatus
    message: str
    attempt: int = 0
    on_retry_override: bool = False
    provisional: bool = False
    raw_value: Any = None


@dataclass(frozen=True)
class RunAggregate:
    """One metric's run-level rollup (§7A.2/§13A). ``value`` is the
    aggregation.method rollup (pass_rate for pass_rate, the percentile for
    p50/p95, …); ``pass_rate`` and ``mean`` are always computed alongside.
    ``n_cases`` is the number of per-case results aggregated (retry-override
    scores excluded); ``n_denominator`` excludes SKIPPED and (with
    on_error: exclude) ERROR results; ``error_n``/``skipped_n`` are reported
    separately, never vanished (§7A.4)."""

    metric_id: str
    value: float | None
    pass_rate: float | None
    mean: float | None
    n_cases: int
    n_denominator: int
    error_n: int = 0
    skipped_n: int = 0
    aggregation_state: AggregationState = AggregationState.COMPLETE


@dataclass(frozen=True)
class GateResult:
    """Run-level gate evaluation (§7A.2) against the aggregated value.
    ``active`` is False when the metric declares no gate (it reports but never
    fails a run). The gate evaluates the point value here; CI-aware gating
    (runs with repeats >= 3 evaluate the CI lower bound, §11C.4) is a later
    chunk."""

    metric_id: str
    value: float | None
    passed: bool
    active: bool
    message: str


class EvaluationError(Exception):
    """The metric cannot be evaluated as given — the case result is ERROR and
    aggregation.on_error decides the run-level rollup (§7A.4). Never silently
    adjusted: an uncompilable predicate, an unsupported selector, a missing
    condition, a composite evaluator without its children — all surface here."""


class ConditionError(EvaluationError):
    """scoring.condition (a predicate over raw_value, §7A.2) does not compile
    in the closed raw_value grammar."""

    def __init__(self, text: str, position: int, message: str):
        self.text = text
        self.position = position
        super().__init__(f"{message} (at position {position})")


class UnsupportedSelectorError(EvaluationError):
    """A target.selector uses JSONPath features outside the supported subset
    (RFC 9535's full grammar is a later chunk)."""


# ---------------------------------------------------------------------------
# Evaluation entry points
# ---------------------------------------------------------------------------


def evaluate_case(
    spec: EvaluationSpec,
    case_id: str,
    events: Sequence[TraceEvent],
    metric: Metric,
    *,
    judge: JudgeEvaluator | None = None,
) -> CaseScore:
    """Score one metric for one case from its trace events.

    First attempts are authoritative (§11C.3): only ``attempt == 0`` events are
    scored; retry events are ignored here (score them with
    :func:`score_attempt` for flakiness analysis — they are flagged
    ``on_retry_override`` and can never re-enter the pass rate).
    """
    _validate_events(case_id, events)
    first_attempt = [e for e in events if e.attempt == 0]
    return score_attempt(spec, case_id, first_attempt, metric, judge=judge)


def score_attempt(
    spec: EvaluationSpec,
    case_id: str,
    events: Sequence[TraceEvent],
    metric: Metric,
    *,
    judge: JudgeEvaluator | None = None,
) -> CaseScore:
    """Score exactly the given events as one attempt (all events must share one
    attempt value). Scores computed from ``attempt > 0`` are flagged
    ``on_retry_override`` — the flag never enters the pass rate (§11C.3)."""
    _validate_events(case_id, events)
    _case_from_spec(spec, case_id)  # raises ValueError for unknown case_id
    if metric.type == "judge" and judge is None:
        raise ValueError(
            "judge metrics require a JudgeEvaluator — never inline an LLM call here"
        )
    if metric.target.type == "run":
        raise ValueError(
            "target.type 'run' metrics are run-scoped (stage-2 scoring, §7A.5); "
            "evaluate_case/score_attempt are per-case"
        )
    attempts = {e.attempt for e in events}
    if len(attempts) > 1:
        raise ValueError(
            f"score_attempt requires events from a single attempt, got attempts {sorted(attempts)}"
        )
    attempt = next(iter(attempts), 0)
    provisional = bool(metric.provisional)
    if not events and metric.target.type != "input":
        # §9A.5: a fully empty trace is governed by on_missing for trace-rule
        # metrics and by the §7A.3 zero-target-match rule for scalar/judge
        # metrics. An `input` target observes the case input in the spec —
        # always observable, never on_missing.
        return _on_missing_result(
            metric,
            case_id,
            attempt=attempt,
            on_retry_override=attempt > 0,
            provisional=provisional,
            reason="the attempt trace is empty (zero events) — §9A.5: on_missing governs",
        )
    try:
        if metric.type == "trace_rule":
            return _score_trace_rule(metric, case_id, events, attempt=attempt,
                                     on_retry_override=attempt > 0, provisional=provisional)
        if metric.type == "scalar":
            return _score_scalar(spec, case_id, events, metric, attempt=attempt,
                                 on_retry_override=attempt > 0, provisional=provisional)
        return _score_judge(spec, case_id, events, metric, judge, attempt=attempt,
                            on_retry_override=attempt > 0, provisional=provisional)
    except (EvaluationError, TraceRuleError, JudgmentError) as exc:
        return _error_result(metric, case_id, str(exc), attempt=attempt,
                             on_retry_override=attempt > 0, provisional=provisional)


def score_output(
    spec: EvaluationSpec,
    case_id: str,
    output: Any,
    metric: Metric,
    *,
    judge: JudgeEvaluator | None = None,
    attempt: int = 0,
    on_retry_override: bool = False,
) -> CaseScore:
    """Score connector/local final-output evidence without fabricating a trace.

    Hosted output-only targets do not claim internal tool/model events. Their
    observed response is still first-class evidence for ``final_response``
    metrics, so it uses the same selector and scoring semantics as trace-backed
    values while preserving an empty event-id set.
    """
    _case_from_spec(spec, case_id)
    provisional = bool(metric.provisional)
    try:
        if metric.type == "judge":
            if judge is None:
                raise JudgmentError("judge metrics require a configured judge")
            if metric.target.type not in _NON_EVENT_TARGETS:
                return score_attempt(
                    spec, case_id, (), metric, judge=judge,
                )
            return _finish_judge(
                metric, case_id, _case_from_spec(spec, case_id), judge, (),
                attempt=attempt, on_retry_override=on_retry_override,
                provisional=provisional,
            )
        if metric.type != "scalar" or metric.target.type not in _NON_EVENT_TARGETS:
            return score_attempt(spec, case_id, (), metric, judge=judge)
        context = output
        if metric.target.type == "final_response" and isinstance(output, dict) and "final_response" in output:
            context = output["final_response"]
        value = _select_from(context, metric.target.selector)
        return _score_observed_value(
            metric, case_id, [value], (), metric.target.type,
            case=_case_from_spec(spec, case_id), attempt=attempt,
            on_retry_override=on_retry_override, provisional=provisional,
        )
    except (EvaluationError, TraceRuleError, JudgmentError) as exc:
        return _error_result(metric, case_id, str(exc), attempt=attempt,
                             on_retry_override=on_retry_override, provisional=provisional)


def aggregate_metric(
    spec: EvaluationSpec,
    metric: Metric,
    scores: Sequence[CaseScore],
    *,
    expected_n: int | None = None,
    in_progress: bool = False,
) -> RunAggregate:
    """Roll per-case results into the run-level aggregate (§7A.2/§13A).

    aggregation_state (§13A.3): IN_PROGRESS when ``in_progress`` (cases still
    executing — a running partial); PARTIAL when fewer scores than
    ``expected_n`` (default: the spec's case count) have landed; COMPLETE
    otherwise. Retry-override scores are dropped — §11C.3: they can never
    re-enter the pass rate. ERROR rollup follows aggregation.on_error (§7A.4):
    ``fail`` counts ERROR as failures, ``exclude`` drops them from the
    denominator and reports ``error_n``. SKIPPED is always excluded from the
    denominator and reported as ``skipped_n``.
    """
    for s in scores:
        if s.metric_id != metric.metric_id:
            raise ValueError(
                f"score for metric {s.metric_id!r} aggregated under metric {metric.metric_id!r}"
            )
    retained = [s for s in scores if not s.on_retry_override]
    if expected_n is None:
        expected_n = len(spec.cases)
    if in_progress:
        state = AggregationState.IN_PROGRESS
    elif len(retained) < expected_n:
        state = AggregationState.PARTIAL
    else:
        state = AggregationState.COMPLETE

    skipped_n = sum(1 for s in retained if s.status is CaseStatus.SKIPPED)
    errors = [s for s in retained if s.status is CaseStatus.ERROR]
    error_n = len(errors)
    if metric.aggregation.on_error == "exclude":
        denominator = [s for s in retained if s.status in (CaseStatus.PASS, CaseStatus.FAIL)]
    else:  # fail — ERROR cases count as failures, denominator includes them
        denominator = [s for s in retained if s.status in (CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.ERROR)]
    if not denominator:
        return RunAggregate(
            metric.metric_id, None, None, None, len(retained), 0, error_n, skipped_n, state
        )
    passes = sum(1 for s in denominator if s.status is CaseStatus.PASS)
    pass_rate = passes / len(denominator)
    # A per-case numeric contribution: the score where there is one, else 0.0
    # (ERROR counted as a failure under on_error: fail contributes zero).
    values = [s.score if s.score is not None else 0.0 for s in denominator]
    method = metric.aggregation.method
    if method == "pass_rate":
        value = pass_rate
    elif method == "mean":
        value = sum(values) / len(values)
    elif method == "p50":
        value = _percentile(values, 0.50)
    elif method == "p95":
        value = _percentile(values, 0.95)
    elif method == "min":
        value = min(values)
    else:  # sum
        value = sum(values)
    mean = sum(values) / len(values)
    return RunAggregate(
        metric.metric_id, value, pass_rate, mean, len(retained), len(denominator),
        error_n, skipped_n, state,
    )


def evaluate_gate(metric: Metric, aggregate: RunAggregate) -> GateResult:
    """Apply the metric's gate (§7A.2) to the aggregated value. A metric with
    no gate reports but never fails a run. Gate-safe metrics only — call
    :func:`gate_safe` first (§15A.1)."""
    gate = metric.gate
    if gate is None:
        return GateResult(metric.metric_id, aggregate.value, True, False, "no gate declared")
    if aggregate.value is None:
        return GateResult(
            metric.metric_id, None, False, True,
            "no aggregated value (empty denominator) — the run cannot pass an unmeasured metric",
        )
    ok = True
    parts = []
    if gate.min is not None and aggregate.value < gate.min:
        ok = False
        parts.append(f"value {aggregate.value} < gate.min {gate.min}")
    if gate.max is not None and aggregate.value > gate.max:
        ok = False
        parts.append(f"value {aggregate.value} > gate.max {gate.max}")
    message = "gate passed" if ok else "gate failed: " + "; ".join(parts)
    return GateResult(metric.metric_id, aggregate.value, ok, True, message)


def gate_safe(
    metrics: Sequence[Metric],
    readiness: JudgeReadinessRegistry | None = None,
) -> list[Metric]:
    """The metrics eligible for gate enforcement (§15A.1, owner decision 4J):
    provisional metrics are excluded — a metric flagged ``provisional`` (the
    shipped default for judge metrics) never gates, and judge metrics whose
    binding is not CALIBRATED never gate either. Deterministic metrics without
    the flag are always gate-safe."""
    out: list[Metric] = []
    for metric in metrics:
        if metric.provisional:
            continue
        if metric.type == "judge":
            if readiness is None:
                continue  # no readiness registry, no calibration evidence — never gate
            if metric.judge_binding is None:
                continue
            if readiness.get(metric.judge_binding).state is not ReadinessState.CALIBRATED:
                continue
        out.append(metric)
    return out


# ---------------------------------------------------------------------------
# Internal plumbing
# ---------------------------------------------------------------------------


def _validate_events(case_id: str, events: Sequence[TraceEvent]) -> None:
    for event in events:
        if not isinstance(event, TraceEvent):
            raise TypeError(
                f"events must be TraceEvent objects, got {type(event).__name__}"
            )
        if event.case_id != case_id:
            raise ValueError(
                f"event {event.event_id!r} belongs to case {event.case_id!r}, "
                f"not {case_id!r} — cross-case contamination is not scored"
            )


def _case_from_spec(spec: EvaluationSpec, case_id: str) -> TestCase:
    for case in spec.cases:
        if case.case_id == case_id:
            return case
    raise ValueError(f"case_id {case_id!r} not found in spec {spec.name!r}")


def _error_result(
    metric: Metric,
    case_id: str,
    message: str,
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    return CaseScore(
        metric.metric_id, case_id, None, False, (), CaseStatus.ERROR,
        f"cannot evaluate: {message}", attempt, on_retry_override, provisional,
    )


def _on_missing_result(
    metric: Metric,
    case_id: str,
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
    reason: str,
) -> CaseScore:
    """§7A.3 on_missing ∈ fail | skip | pass — the G6 decision. Explicitly
    written, never defaulted (the spec model enforces presence)."""
    on_missing = metric.target.on_missing
    if on_missing == "pass":
        return CaseScore(
            metric.metric_id, case_id, 1.0, True, (), CaseStatus.PASS,
            f"on_missing=pass ({reason})", attempt, on_retry_override, provisional,
        )
    if on_missing == "skip":
        return CaseScore(
            metric.metric_id, case_id, None, False, (), CaseStatus.SKIPPED,
            f"on_missing=skip ({reason})", attempt, on_retry_override, provisional,
        )
    if on_missing == "fail":
        return CaseScore(
            metric.metric_id, case_id, 0.0, False, (), CaseStatus.FAIL,
            f"on_missing=fail ({reason})", attempt, on_retry_override, provisional,
        )
    raise EvaluationError(
        f"target.on_missing must be fail|skip|pass, got {on_missing!r} (§7A.3 — no default)"
    )


def _dedupe_ids(ids: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return tuple(out)


def _percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile over the sorted scores (deterministic)."""
    sorted_values = sorted(values)
    n = len(sorted_values)
    k = math.ceil(p * n) - 1
    return sorted_values[max(0, min(k, n - 1))]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _typed_eq(a: Any, b: Any) -> bool:
    """Typed equality for observed vs expected values: bools only with bools,
    numbers numerically, everything else by identical type (mirrors the §9A
    predicate language's comparison core; a MISSING operand never equals)."""
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if _is_number(a) and _is_number(b):
        return a == b  # 5 == 5.0
    return type(a) is type(b) and a == b


# ---------------------------------------------------------------------------
# scoring.condition — the closed raw_value grammar (§7A.2)
# ---------------------------------------------------------------------------

#: scoring.condition is a predicate over ``raw_value`` (the §9A.2 metric-level
#: CEL variable). Full CEL lands with cel-python; until then the condition is
#: the closed grammar below — comparisons between ``raw_value`` and literals,
#: combined with && || ! and parentheses. Everything else is a compile error
#: (ConditionError), never silently accepted and never eval()'d.

_COND_COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")


class _Condition:
    """A compiled scoring.condition. ``evaluate(raw)`` is total: returns a
    bool; MISSING or UNKNOWN operands render False (the §9A missing-value rule
    — a missing value never passes a check by accident)."""

    def __init__(self, text: str):
        if not isinstance(text, str):
            raise TypeError(f"condition must be a str, got {type(text).__name__}")
        if not text.strip():
            raise ConditionError(text, 0, "condition is empty")
        self.text = text
        self._ast = _CondParser(text).parse()

    @classmethod
    def compile(cls, text: str) -> "_Condition":
        return cls(text)

    def evaluate(self, raw: Any) -> bool:
        return self._ast.evaluate(raw) is True


class _CondNode:
    __slots__ = ()

    def evaluate(self, raw: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class _CondLiteral(_CondNode):
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value

    def evaluate(self, raw: Any) -> Any:
        return self.value


class _CondRaw(_CondNode):
    __slots__ = ()

    def evaluate(self, raw: Any) -> Any:
        return raw if raw is not MISSING else MISSING


class _CondCompare(_CondNode):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: _CondNode, right: _CondNode):
        self.op = op
        self.left = left
        self.right = right

    def evaluate(self, raw: Any) -> Any:
        return _cond_compare(self.op, self.left.evaluate(raw), self.right.evaluate(raw))


class _CondBool(_CondNode):
    __slots__ = ("op", "children")

    def __init__(self, op: str, children: list[_CondNode]):
        self.op = op
        self.children = children

    def evaluate(self, raw: Any) -> Any:
        if self.op == "&&":
            result: Any = True
            for child in self.children:
                result = _cond_and(result, child.evaluate(raw))
            return result
        result = False
        for child in self.children:
            result = _cond_or(result, child.evaluate(raw))
        return result


class _CondNot(_CondNode):
    __slots__ = ("child",)

    def __init__(self, child: _CondNode):
        self.child = child

    def evaluate(self, raw: Any) -> Any:
        return _cond_not(self.child.evaluate(raw))


def _cond_compare(op: str, a: Any, b: Any) -> Any:
    """Three-valued comparison with the §9A missing-value rule: MISSING
    operands give UNKNOWN except ``raw_value == null`` with raw MISSING
    (True)."""
    if a is MISSING or b is MISSING:
        if op == "==" and (a is None or b is None):
            return True
        return UNKNOWN
    if op == "==":
        return _typed_eq(a, b)
    if op == "!=":
        return not _typed_eq(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        return UNKNOWN
    if _is_number(a) and _is_number(b):
        ordered = (a > b) - (a < b)
    elif isinstance(a, str) and isinstance(b, str):
        ordered = (a > b) - (a < b)
    else:
        return UNKNOWN
    if op == "<":
        return ordered < 0
    if op == "<=":
        return ordered <= 0
    if op == ">":
        return ordered > 0
    return ordered >= 0


def _cond_and(a: Any, b: Any) -> Any:
    if a is False or b is False:
        return False
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return True


def _cond_or(a: Any, b: Any) -> Any:
    if a is True or b is True:
        return True
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return False


def _cond_not(a: Any) -> Any:
    if a is UNKNOWN:
        return UNKNOWN
    return not a


class _CondParser:
    """Recursive descent over the closed raw_value grammar:

        expr := and ("||" and)*
        and := not ("&&" not)*
        not := "!" not | cmp
        cmp := operand (comp_op operand)?
        operand := "raw_value" | literal | "(" expr ")"
    """

    def __init__(self, text: str):
        self.text = text
        self.tokens = self._tokenize(text)
        self.pos = 0

    def _error(self, pos: int, message: str) -> ConditionError:
        return ConditionError(self.text, pos, message)

    def _tokenize(self, text: str) -> list[tuple[str, Any, int]]:
        tokens: list[tuple[str, Any, int]] = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c in " \t\r\n":
                i += 1
                continue
            two = text[i : i + 2]
            if two in ("&&", "||", "==", "!=", "<=", ">="):
                tokens.append(("op", two, i))
                i += 2
                continue
            if c in "<>!":
                tokens.append(("op", c, i))
                i += 1
                continue
            if c in "()":
                tokens.append(("paren", c, i))
                i += 1
                continue
            if c in "\"'":
                value, end = self._scan_string(text, i)
                tokens.append(("literal", value, i))
                i = end
                continue
            if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
                value, end = self._scan_number(text, i)
                tokens.append(("literal", value, i))
                i = end
                continue
            if c.isalpha() or c == "_":
                j = i + 1
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                tokens.append(("ident", text[i:j], i))
                i = j
                continue
            raise self._error(i, f"unexpected character {c!r}")
        tokens.append(("eof", None, n))
        return tokens

    def _scan_string(self, text: str, start: int) -> tuple[str, int]:
        quote = text[start]
        i = start + 1
        n = len(text)
        out: list[str] = []
        while i < n:
            c = text[i]
            if c == quote:
                return "".join(out), i + 1
            if c == "\\" and i + 1 < n and text[i + 1] in ("\\", '"', "'", "n", "t", "r"):
                esc = text[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc))
                i += 2
                continue
            out.append(c)
            i += 1
        raise self._error(start, "unterminated string literal")

    def _scan_number(self, text: str, start: int) -> tuple[int | float, int]:
        i = start
        n = len(text)
        if text[i] == "-":
            i += 1
        while i < n and text[i].isdigit():
            i += 1
        is_float = False
        if i < n and text[i] == ".":
            is_float = True
            i += 1
            while i < n and text[i].isdigit():
                i += 1
        raw = text[start:i]
        return (float(raw) if is_float else int(raw)), i

    def peek(self) -> tuple[str, Any, int]:
        return self.tokens[self.pos]

    def advance(self) -> tuple[str, Any, int]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> _CondNode:
        node = self._parse_or()
        kind, value, pos = self.peek()
        if kind != "eof":
            raise self._error(pos, f"unexpected trailing input {value!r}")
        return node

    def _parse_or(self) -> _CondNode:
        nodes = [self._parse_and()]
        while self.peek()[0] == "op" and self.peek()[1] == "||":
            self.advance()
            nodes.append(self._parse_and())
        if len(nodes) == 1:
            return nodes[0]
        return _CondBool("||", nodes)

    def _parse_and(self) -> _CondNode:
        nodes = [self._parse_not()]
        while self.peek()[0] == "op" and self.peek()[1] == "&&":
            self.advance()
            nodes.append(self._parse_not())
        if len(nodes) == 1:
            return nodes[0]
        return _CondBool("&&", nodes)

    def _parse_not(self) -> _CondNode:
        if self.peek()[0] == "op" and self.peek()[1] == "!":
            self.advance()
            return _CondNot(self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self) -> _CondNode:
        left = self._parse_operand()
        kind, value, pos = self.peek()
        if kind == "op" and value in _COND_COMPARISONS:
            self.advance()
            right = self._parse_operand()
            return _CondCompare(value, left, right)
        return left

    def _parse_operand(self) -> _CondNode:
        kind, value, pos = self.peek()
        if kind == "literal":
            self.advance()
            return _CondLiteral(value)
        if kind == "ident":
            if value == "raw_value":
                self.advance()
                return _CondRaw()
            if value == "true":
                self.advance()
                return _CondLiteral(True)
            if value == "false":
                self.advance()
                return _CondLiteral(False)
            if value == "null":
                self.advance()
                return _CondLiteral(None)
            raise self._error(
                pos,
                f"unknown identifier {value!r}; a condition may reference only "
                "'raw_value' and literals (§9A.2 raw_value)",
            )
        if kind == "paren" and value == "(":
            self.advance()
            node = self._parse_or()
            kind, value, pos = self.peek()
            if not (kind == "paren" and value == ")"):
                raise self._error(pos, "expected ')' to close the parenthesized expression")
            self.advance()
            return node
        raise self._error(pos, f"expected 'raw_value' or a literal, found {value!r}")


# ---------------------------------------------------------------------------
# Target selection — the §7A.3 observation surface
# ---------------------------------------------------------------------------

#: target types whose value lives outside the trace: the final response is the
#: validated field of /output/result.json (§9A.1), state_change/workflow_node
#: are run-manifest properties, external needs a resolver. The trace-only
#: evaluator matches zero events for these and on_missing decides (§7A.3).
_NON_EVENT_TARGETS = frozenset(
    {"final_response", "state_change", "workflow_node", "external"}
)


def _select_target_events(target: Any, events: Sequence[TraceEvent]) -> list[TraceEvent]:
    """The events a target observes (scalar/judge metrics): tool_call for
    tool_invocation/tool_arguments, tool_result for tool_output, llm_response
    for model_output. Ordered by (timestamp, sequence) — deterministic."""
    t = target.type
    if t == "model_output":
        event_type = EventType.LLM_RESPONSE
    elif t in ("tool_invocation", "tool_arguments"):
        event_type = EventType.TOOL_CALL
    elif t == "tool_output":
        event_type = EventType.TOOL_RESULT
    else:
        return []
    out = [e for e in events if e.type is event_type and (target.tool is None or e.tool == target.tool)]
    out.sort(key=lambda e: (e.timestamp, e.sequence))
    return out


def _apply_occurrence(occurrence: str | None, selected: Sequence[TraceEvent]) -> list[TraceEvent] | None:
    """§7A.3 occurrence ∈ first | last | all | index:N, by (timestamp, sequence).
    index:N out of range returns None — it behaves exactly as on_missing."""
    if occurrence in (None, "first"):
        return [selected[0]]
    if occurrence == "last":
        return [selected[-1]]
    if occurrence == "all":
        return list(selected)
    if isinstance(occurrence, str) and occurrence.startswith("index:"):
        idx = int(occurrence.split(":", 1)[1])
        if idx >= len(selected):
            return None
        return [selected[idx]]
    raise EvaluationError(f"unknown occurrence {occurrence!r} (§7A.3: first|last|all|index:N)")


def _payload_ctx(event: TraceEvent, key: str) -> Any:
    if event.payload is None:
        return MISSING
    return event.payload.get(key, MISSING)


def _select_from(context: Any, selector: str | None) -> Any:
    """Resolve the target's JSONPath selector against the observation context
    (the §9A.2 alias context for event targets: payload.args for
    tool_arguments, payload.output for tool_output, payload.content for
    model_output, payload for tool_invocation; case.input for input targets).
    Supported subset: ``$``, dotted keys, ``[N]`` index, ``.*`` / ``[*]``
    wildcards. Unresolved paths yield MISSING (never a silent pass); anything
    outside the subset is an UnsupportedSelectorError."""
    if selector is None:
        return context
    if not isinstance(selector, str) or not selector.startswith("$"):
        raise UnsupportedSelectorError(
            f"selector must be a JSONPath starting with '$', got {selector!r}"
        )
    tokens = _selector_tokens(selector)
    value = context
    for tok in tokens:
        if tok == "*":
            if isinstance(value, dict):
                value = list(value.values())
            elif isinstance(value, list):
                value = list(value)
            else:
                return MISSING
        elif isinstance(tok, int):
            if isinstance(value, list) and 0 <= tok < len(value):
                value = value[tok]
            else:
                return MISSING
        else:  # key
            if isinstance(value, dict) and tok in value:
                value = value[tok]
            else:
                return MISSING
    return value


def _selector_tokens(selector: str) -> list[Any]:
    """Tokenize the supported JSONPath subset. Raises UnsupportedSelectorError
    for anything outside it (filters, recursive descent, unions, functions)."""
    rest = selector[1:]  # strip '$'
    tokens: list[Any] = []
    i, n = 0, len(rest)
    while i < n:
        c = rest[i]
        if c == ".":
            if i + 1 < n and rest[i + 1] == ".":
                raise UnsupportedSelectorError(
                    f"recursive descent '$..' is not in the supported subset: {selector!r}"
                )
            if i + 1 < n and rest[i + 1] == "*":
                tokens.append("*")
                i += 2
                continue
            j = i + 1
            while j < n and (rest[j].isalnum() or rest[j] == "_"):
                j += 1
            if j == i + 1:
                raise UnsupportedSelectorError(
                    f"unsupported selector segment near {selector[: j + 1]!r}"
                )
            tokens.append(rest[i + 1 : j])
            i = j
            continue
        if c == "[":
            close = rest.find("]", i)
            if close == -1:
                raise UnsupportedSelectorError(f"unterminated '[' in selector {selector!r}")
            inner = rest[i + 1 : close].strip()
            if inner == "*":
                tokens.append("*")
            elif inner.isdigit():
                tokens.append(int(inner))
            else:
                raise UnsupportedSelectorError(
                    f"unsupported selector bracket {inner!r}; supported: [N], [*] ({selector!r})"
                )
            i = close + 1
            continue
        raise UnsupportedSelectorError(
            f"unsupported selector character {c!r} in {selector!r}; "
            "supported subset: $, .key, [N], .*, [*]"
        )
    return tokens


# ---------------------------------------------------------------------------
# Metric-type scoring
# ---------------------------------------------------------------------------


def _score_trace_rule(
    metric: Metric,
    case_id: str,
    events: Sequence[TraceEvent],
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    result = evaluate_rule(metric.evaluator.rule, events)
    rule = metric.evaluator.rule
    if rule.get("op") == "count":
        # Terminal numeric rule: the count is raw_value; per-case pass/fail
        # comes from scoring.condition (§9A.3/§7A.2).
        return _score_raw_value(
            metric, case_id, result.value, result.matched,
            attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional, what="count",
        )
    if result.result:
        return CaseScore(
            metric.metric_id, case_id, 1.0, True, result.matched, CaseStatus.PASS,
            f"rule passed (matched {len(result.matched)} events)",
            attempt, on_retry_override, provisional, result.result,
        )
    evidence = _dedupe_ids((*result.matched, *result.failing))
    return CaseScore(
        metric.metric_id, case_id, 0.0, False, evidence, CaseStatus.FAIL,
        "rule failed", attempt, on_retry_override, provisional, result.result,
    )


def _score_raw_value(
    metric: Metric,
    case_id: str,
    raw: Any,
    evidence: tuple[str, ...],
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
    what: str,
) -> CaseScore:
    """Map a raw value (or list, for occurrence all) to a per-case score per
    scoring.type (§7A.2)."""
    scoring = metric.scoring
    if isinstance(raw, list):
        # §7A.3 occurrence all: with binary scoring the case passes iff every
        # selected event passes; with numeric scoring the raw value is the list.
        if scoring.type == "binary":
            passed = all(_eval_condition(metric, v) for v in raw)
            return CaseScore(
                metric.metric_id, case_id, 1.0 if passed else 0.0, passed, evidence,
                CaseStatus.PASS if passed else CaseStatus.FAIL,
                f"{what}: every selected value must satisfy the condition "
                f"(got {len(raw)} values)",
                attempt, on_retry_override, provisional, _safe_raw_value(raw),
            )
        if scoring.type == "numeric":
            norms = [_normalize_score(scoring, v) for v in raw]
            in_range = [_in_score_range(scoring, v) for v in raw]
            passed = all(in_range)
            score = sum(norms) / len(norms)
            return CaseScore(
                metric.metric_id, case_id, score, passed, evidence,
                CaseStatus.PASS if passed else CaseStatus.FAIL,
                f"{what}: normalized mean over {len(raw)} selected values",
                attempt, on_retry_override, provisional, _safe_raw_value(raw),
            )
        raise EvaluationError(
            f"categorical scoring cannot consume a list of values ({what})"
        )
    if scoring.type == "binary":
        passed = _eval_condition(metric, raw)
        return CaseScore(
            metric.metric_id, case_id, 1.0 if passed else 0.0, passed, evidence,
            CaseStatus.PASS if passed else CaseStatus.FAIL,
            f"{what}: condition over raw_value={raw!r} "
            + ("passed" if passed else "failed"),
            attempt, on_retry_override, provisional, _safe_raw_value(raw),
        )
    if scoring.type == "numeric":
        passed = _in_score_range(scoring, raw)
        score = _normalize_score(scoring, raw)
        return CaseScore(
            metric.metric_id, case_id, score, passed, evidence,
            CaseStatus.PASS if passed else CaseStatus.FAIL,
            f"{what}: raw_value={raw!r} "
            + (f"within range {scoring.range}" if passed else f"outside range {scoring.range}"),
            attempt, on_retry_override, provisional, _safe_raw_value(raw),
        )
    if scoring.type == "categorical":
        categories = scoring.categories or []
        raw_s = str(raw) if raw is not MISSING else None
        if raw_s in categories:
            idx = categories.index(raw_s)
            score = idx / (len(categories) - 1) if len(categories) > 1 else 1.0
            passed = True
        else:
            score, passed = 0.0, False
        return CaseScore(
            metric.metric_id, case_id, score, passed, evidence,
            CaseStatus.PASS if passed else CaseStatus.FAIL,
            f"{what}: value {raw_s!r} "
            + ("in categories" if passed else f"not in categories {categories}"),
            attempt, on_retry_override, provisional, _safe_raw_value(raw),
        )
    raise EvaluationError(f"unsupported scoring.type {scoring.type!r} (§7A.2)")


def _safe_raw_value(value: Any) -> Any:
    """Make missing sentinels representable in JSON result evidence."""
    if value is MISSING or value is UNKNOWN:
        return None
    if isinstance(value, list):
        return [_safe_raw_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_raw_value(item) for key, item in value.items()}
    return value


def _eval_condition(metric: Metric, raw: Any) -> bool:
    """§7A.2: binary scoring of a numeric-producing evaluator requires
    scoring.condition — never defaulted. Missing or uncompilable conditions are
    EvaluationError (the case is ERROR, on_error decides)."""
    condition = metric.scoring.condition
    if not condition:
        raise EvaluationError(
            f"binary scoring of {metric.evaluator.type!r} requires scoring.condition "
            "(a predicate over raw_value, §7A.2 — never defaulted)"
        )
    return _Condition.compile(condition).evaluate(raw)


def _in_score_range(scoring: Any, raw: Any) -> bool:
    if not _is_number(raw):
        return False
    lo, hi = scoring.range
    return lo <= raw <= hi


def _normalize_score(scoring: Any, raw: Any) -> float:
    """normalized_score into [0, 1] for weight composition (§7A.2). A
    non-numeric raw never passes and scores 0.0."""
    if not _is_number(raw):
        return 0.0
    lo, hi = scoring.range
    if hi > lo:
        return min(max((raw - lo) / (hi - lo), 0.0), 1.0)
    return 1.0 if raw == lo else 0.0


def _score_scalar(
    spec: EvaluationSpec,
    case_id: str,
    events: Sequence[TraceEvent],
    metric: Metric,
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    target = metric.target
    t = target.type
    if t == "run":
        raise ValueError(
            "target.type 'run' metrics are run-scoped (stage-2 scoring, §7A.5)"
        )
    if t == "trace":
        raise EvaluationError(
            "a scalar metric cannot evaluate a whole trace; use a trace_rule metric (§9A.3)"
        )
    if t == "input":
        # The case input is in the spec — always observed, never on_missing.
        case = _case_from_spec(spec, case_id)
        value = _select_from(case.input, target.selector)
        return _score_observed_value(metric, case_id, [value], (), t, case=case,
                                     attempt=attempt, on_retry_override=on_retry_override,
                                     provisional=provisional)
    if t in _NON_EVENT_TARGETS:
        # The observed surface lives outside the trace (§9A.1) — the trace-only
        # evaluator matches zero events and on_missing decides (§7A.3).
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional,
            reason=f"target.type {t!r} observes a value outside the trace",
        )
    selected = _select_target_events(target, events)
    if not selected:
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional, reason="the target matched zero events (§7A.3)",
        )
    sel = _apply_occurrence(target.occurrence, selected)
    if sel is None:
        # §7A.3: index:N out of range behaves exactly as on_missing.
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional, reason="occurrence index:N out of range (§7A.3)",
        )
    evaluator = metric.evaluator
    if evaluator.type == "cel_predicate":
        try:
            pred = Predicate.compile(evaluator.predicate)
        except PredicateError as exc:
            raise EvaluationError(f"cel_predicate does not compile: {exc}") from exc
        passed = all(pred.evaluate(e) for e in sel)
        return _boolean_case_score(metric, case_id, passed, _ids_of(sel), f"cel_predicate",
                                   attempt=attempt, on_retry_override=on_retry_override,
                                   provisional=provisional)
    # Value-producing evaluators: extract the observed value per §7A.3/§9A.2
    # alias contexts.
    values: list[Any] = []
    for event in sel:
        if t == "tool_arguments":
            ctx = _payload_ctx(event, "args")
        elif t == "tool_output":
            ctx = _payload_ctx(event, "output")
        elif t == "model_output":
            ctx = _payload_ctx(event, "content")
        else:  # tool_invocation
            ctx = event.payload
        values.append(_select_from(ctx, target.selector))
    return _score_observed_value(metric, case_id, values, _ids_of(sel), t,
                                 case=_case_from_spec(spec, case_id),
                                 attempt=attempt, on_retry_override=on_retry_override,
                                 provisional=provisional)


def _ids_of(events: Sequence[TraceEvent]) -> tuple[str, ...]:
    return tuple(e.event_id for e in events)


def _score_observed_value(
    metric: Metric,
    case_id: str,
    values: Sequence[Any],
    evidence: tuple[str, ...],
    target_type: str,
    *,
    case: TestCase | None = None,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    """exact_match / regex / numeric / reference over the observed value(s)."""
    evaluator = metric.evaluator
    ev_type = evaluator.type
    if ev_type == "exact_match":
        expected = evaluator.expected
        if metric.scoring.type == "binary":
            passed = _typed_eq(values[0], expected) if len(values) == 1 else all(
                _typed_eq(v, expected) for v in values
            )
            return _boolean_case_score(metric, case_id, passed, evidence, "exact_match",
                                       attempt=attempt, on_retry_override=on_retry_override,
                                       provisional=provisional)
        raw = values[0] if len(values) == 1 else list(values)
        return _score_raw_value(metric, case_id, raw, evidence,
                                attempt=attempt, on_retry_override=on_retry_override,
                                provisional=provisional, what=f"exact_match ({target_type})")
    if ev_type == "regex":
        pattern = evaluator.pattern
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise EvaluationError(f"regex evaluator pattern does not compile: {exc}") from exc
        if metric.scoring.type == "binary":
            passed = all(
                isinstance(v, str) and compiled.search(v) is not None for v in values
            )
            return _boolean_case_score(metric, case_id, passed, evidence, "regex",
                                       attempt=attempt, on_retry_override=on_retry_override,
                                       provisional=provisional)
        raw = values[0] if len(values) == 1 else list(values)
        return _score_raw_value(metric, case_id, raw, evidence,
                                attempt=attempt, on_retry_override=on_retry_override,
                                provisional=provisional, what="regex")
    if ev_type == "numeric":
        raw = values[0] if len(values) == 1 else list(values)
        return _score_raw_value(metric, case_id, raw, evidence,
                                attempt=attempt, on_retry_override=on_retry_override,
                                provisional=provisional, what=f"numeric ({target_type})")
    if ev_type == "recall_at_k":
        raw = values[0] if len(values) == 1 else list(values)
        if not isinstance(raw, list):
            raise EvaluationError("recall_at_k requires a ranked string list")
        if any(not isinstance(item, str) for item in raw):
            raise EvaluationError("recall_at_k ranked values must be strings")
        score = recall_at_k(evaluator.expected, raw, evaluator.k)
        return _score_raw_value(
            metric, case_id, score, evidence, attempt=attempt,
            on_retry_override=on_retry_override, provisional=provisional,
            what="recall_at_k",
        )
    if ev_type == "citation_correctness":
        raw = values[0] if len(values) == 1 else list(values)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise EvaluationError("citation_correctness requires a citation string list")
        score = citation_correctness(raw, evaluator.evidence or {})
        if score is None:
            raise EvaluationError("citation evidence is unavailable")
        return _score_raw_value(
            metric, case_id, score, evidence, attempt=attempt,
            on_retry_override=on_retry_override, provisional=provisional,
            what="citation_correctness",
        )
    if ev_type == "reference":
        if case is None:
            raise EvaluationError("reference evaluator requires the test case")
        expected_ref = _resolve_reference(metric, case)
        if metric.scoring.type == "binary":
            passed = _typed_eq(values[0], expected_ref) if len(values) == 1 else all(
                _typed_eq(v, expected_ref) for v in values
            )
            return _boolean_case_score(metric, case_id, passed, evidence, "reference",
                                       attempt=attempt, on_retry_override=on_retry_override,
                                       provisional=provisional)
        raw = values[0] if len(values) == 1 else list(values)
        return _score_raw_value(metric, case_id, raw, evidence,
                                attempt=attempt, on_retry_override=on_retry_override,
                                provisional=provisional, what="reference")
    if ev_type == "json_schema":
        schema = evaluator.schema
        if metric.scoring.type == "binary":
            passed = all(
                _validate_json_schema(schema, v) for v in values
            )
            return _boolean_case_score(metric, case_id, passed, evidence, "json_schema",
                                       attempt=attempt, on_retry_override=on_retry_override,
                                       provisional=provisional)
        raw = values[0] if len(values) == 1 else list(values)
        return _score_raw_value(metric, case_id, raw, evidence,
                                attempt=attempt, on_retry_override=on_retry_override,
                                provisional=provisional, what="json_schema")
    if ev_type == "composite":
        raise EvaluationError(
            "composite evaluators need their child metric scores; the schema has no "
            "composite children field yet (§7A.2) — wire it before evaluating"
        )
    raise EvaluationError(
        f"scalar metric with evaluator.type {ev_type!r} cannot be evaluated here"
    )


def _boolean_case_score(
    metric: Metric,
    case_id: str,
    passed: bool,
    evidence: tuple[str, ...],
    what: str,
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
    raw_value: Any = None,
) -> CaseScore:
    """A boolean evaluator under binary scoring passes iff it returns true
    (§7A.2). Non-binary scoring over a boolean feeds 1.0/0.0 as the raw value."""
    if metric.scoring.type == "binary":
        return CaseScore(
            metric.metric_id, case_id, 1.0 if passed else 0.0, passed, evidence,
            CaseStatus.PASS if passed else CaseStatus.FAIL,
            f"{what} " + ("passed" if passed else "failed"),
            attempt, on_retry_override, provisional,
            passed if raw_value is None else raw_value,
        )
    return _score_raw_value(metric, case_id, 1.0 if passed else 0.0, evidence,
                            attempt=attempt, on_retry_override=on_retry_override,
                            provisional=provisional, what=what)


def _validate_json_schema(schema: Any, instance: Any) -> bool:
    """Minimal draft-07 subset for the json_schema evaluator (§7A.2): type,
    properties, required, items, enum, const, minimum, maximum, minLength,
    maxLength, pattern. Unknown keywords are ignored per the spec (a schema
    written for a fuller implementation never fails an instance for keywords
    we do not implement). Anything outside the subset's instance surface is a
    non-match (False), never an error — a missing selector result never
    passes."""
    if not isinstance(schema, dict):
        raise EvaluationError(f"json_schema evaluator requires a JSON Schema object, got {schema!r}")
    return _json_ok(schema, instance)


def _json_ok(schema: Any, instance: Any) -> bool:
    if schema is True:
        return True
    if schema is False or not isinstance(schema, dict):
        return False
    # type
    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, list):
            if not any(_matches_json_type(t, instance) for t in expected):
                return False
        elif not _matches_json_type(expected, instance):
            return False
    # enum / const
    if "enum" in schema:
        if not any(_typed_eq(e, instance) for e in schema["enum"]):
            return False
    if "const" in schema and not _typed_eq(schema["const"], instance):
        return False
    # numeric bounds
    if _is_number(instance):
        if schema.get("minimum") is not None and not (instance >= schema["minimum"]):
            return False
        if schema.get("maximum") is not None and not (instance <= schema["maximum"]):
            return False
    # string constraints
    if isinstance(instance, str):
        if schema.get("minLength") is not None and len(instance) < schema["minLength"]:
            return False
        if schema.get("maxLength") is not None and len(instance) > schema["maxLength"]:
            return False
        if schema.get("pattern") is not None:
            try:
                if re.search(schema["pattern"], instance) is None:
                    return False
            except re.error as exc:
                raise EvaluationError(f"json_schema pattern does not compile: {exc}") from exc
    # objects: properties + required
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                return False
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance and not _json_ok(subschema, instance[key]):
                return False
    # arrays: items
    if isinstance(instance, list):
        items = schema.get("items")
        if items is not None:
            for element in instance:
                if not _json_ok(items, element):
                    return False
    return True


def _matches_json_type(expected: str, instance: Any) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "number":
        return _is_number(instance)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def _resolve_reference(metric: Metric, case: TestCase) -> Any:
    """reference evaluator: a dotted path into the test case's expected
    (§7A.2), e.g. 'test.expected.refund_amount' resolves to
    case.expected['refund_amount'].value. Further segments descend into the
    expected value when it is a dict (e.g.
    'test.expected.address.city'). An unresolvable path means the metric
    cannot be evaluated — the case result is ERROR and on_error decides
    (§7A.4)."""
    path = metric.evaluator.expected
    if not isinstance(path, str):
        raise EvaluationError(f"reference evaluator requires a field path, got {path!r}")
    segments = path.split(".")
    if len(segments) < 3 or segments[:2] != ["test", "expected"]:
        raise EvaluationError(
            f"reference path {path!r} must start with 'test.expected' (§7A.2)"
        )
    key = segments[2]
    if key not in case.expected:
        raise EvaluationError(
            f"reference path {path!r}: no expected value {key!r} on case {case.case_id!r}"
        )
    value = case.expected[key].value
    for segment in segments[3:]:
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        else:
            raise EvaluationError(
                f"reference path {path!r}: {segment!r} does not resolve "
                f"(expected value for {key!r} is {value!r})"
            )
    return value


def _score_judge(
    spec: EvaluationSpec,
    case_id: str,
    events: Sequence[TraceEvent],
    metric: Metric,
    judge: JudgeEvaluator,
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    target = metric.target
    if target.type in _NON_EVENT_TARGETS or target.type in ("trace", "run"):
        # The judge's content is the candidate response + bounded context; the
        # trace-only evaluator observes no such value here -> on_missing.
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional,
            reason=f"target.type {target.type!r} observes a value outside the trace",
        )
    if target.type == "input":
        # The case input is in the spec — judge it against nothing: dispatch
        # with empty evidence (a later chunk wires the content assembly).
        case = _case_from_spec(spec, case_id)
        evidence: tuple[str, ...] = ()
        return _finish_judge(metric, case_id, case, judge, evidence,
                             attempt=attempt, on_retry_override=on_retry_override,
                             provisional=provisional)
    selected = _select_target_events(target, events)
    if not selected:
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional, reason="the target matched zero events (§7A.3)",
        )
    sel = _apply_occurrence(target.occurrence, selected)
    if sel is None:
        return _on_missing_result(
            metric, case_id, attempt=attempt, on_retry_override=on_retry_override,
            provisional=provisional, reason="occurrence index:N out of range (§7A.3)",
        )
    return _finish_judge(metric, case_id, _case_from_spec(spec, case_id), judge,
                         _ids_of(sel), attempt=attempt,
                         on_retry_override=on_retry_override, provisional=provisional)


def _finish_judge(
    metric: Metric,
    case_id: str,
    case: TestCase,
    judge: JudgeEvaluator,
    evidence: tuple[str, ...],
    *,
    attempt: int,
    on_retry_override: bool,
    provisional: bool,
) -> CaseScore:
    try:
        verdict = judge.evaluate(metric, case, evidence)
    except JudgmentError as exc:
        raise JudgmentError(f"judge verdict invalid: {exc}") from exc
    raw = verdict.score
    # §15A.2: a score outside the rubric's declared scale (scoring.range) is
    # an ERROR result, never a coerced score.
    if metric.scoring.range is not None:
        lo, hi = metric.scoring.range
        if not (lo <= raw <= hi):
            raise JudgmentError(
                f"judge score {raw!r} outside the rubric scale {metric.scoring.range!r}"
            )
    combined_evidence = _dedupe_ids((*evidence, *verdict.evidence_refs))
    return _score_raw_value(
        metric, case_id, raw, combined_evidence,
        attempt=attempt, on_retry_override=on_retry_override,
        provisional=provisional, what=f"judge ({judge.binding.model})",
    )
