"""Tests for per-case scoring and run aggregation (src/llm_agent_eval/evaluators.py).

Covers the §7A three-layer separation (per-case CaseScore, run RunAggregate,
gate), first-attempt authority (§11C.3: retries never re-enter the pass rate),
the §9A.5 empty-trace on_missing rule (a fully empty trace is the only case
on_missing governs for trace-rule metrics), the §7A.3 zero-match on_missing
for scalar/judge metrics, on_error rollups (§7A.4: fail | exclude), §13A
aggregation_state (PARTIAL when cases are missing — never fakes complete),
judge dispatch through the JudgeEvaluator interface (never an inline LLM
call), and gate safety (§15A.1: provisional and non-CALIBRATED judge metrics
never gate).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llm_agent_eval.evaluators import (
    AggregationState,
    CaseScore,
    CaseStatus,
    RunAggregate,
    aggregate_metric,
    evaluate_case,
    evaluate_gate,
    gate_safe,
    score_attempt,
)
from llm_agent_eval.events import make_event
from llm_agent_eval.judge import JudgeReadiness, JudgeReadinessRegistry, ScriptedJudge
from llm_agent_eval.spec import EvaluationSpec, JudgeBinding, validate_spec

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

BINDING = JudgeBinding(
    provider="anthropic",
    model="claude-opus-5",
    schema_version="1",
    rubric_version="1",
)


def ev(
    event_type,
    event_id,
    *,
    ts=0,
    sequence=0,
    source="proxy",
    tool=None,
    parent=None,
    payload=None,
    attempt=0,
):
    kw = {}
    if tool is not None:
        kw["tool"] = tool
    if parent is not None:
        kw["parent_event_id"] = parent
    if payload is not None:
        kw["payload"] = payload
    return make_event(
        event_type=event_type,
        run_id="run1",
        case_id="c1",
        workspace_id="ws1",
        attempt_id=f"a{attempt}",
        attempt=attempt,
        event_id=event_id,
        timestamp=T0 + timedelta(milliseconds=ts),
        sequence=sequence,
        source=source,
        **kw,
    )


def make_spec(**overrides):
    """A valid two-case spec; metrics are replaced per test."""
    spec = {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "cases": [
            {"case_id": "c1", "name": "one", "input": {"order_id": "123"}},
            {"case_id": "c2", "name": "two", "input": {"order_id": "456"}},
        ],
        "metrics": [
            {
                "metric_id": "refund_amount",
                "name": "refund amount correct",
                "type": "scalar",
                "target": {
                    "type": "tool_output",
                    "tool": "refund_order",
                    "selector": "$.amount",
                    "occurrence": "last",
                    "on_missing": "fail",
                },
                "evaluator": {"type": "numeric", "expected": 49.99, "tolerance": 0},
                "scoring": {"type": "binary", "range": [0, 1], "condition": "raw_value <= 50"},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "gate": {"min": 1.0},
                "weight": 1.0,
                "provisional": False,
            }
        ],
    }
    spec.update(overrides)
    return spec


def scalar_metric(**overrides):
    metric = {
        "metric_id": "m1",
        "name": "m1",
        "type": "scalar",
        "target": {
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        "evaluator": {"type": "exact_match", "expected": 49.99},
        "scoring": {"type": "binary", "range": [0, 1]},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "provisional": False,
    }
    metric.update(overrides)
    return metric


def trace_rule_metric(**overrides):
    metric = {
        "metric_id": "never_budget_exceeded",
        "name": "no budget abort",
        "type": "trace_rule",
        "target": {"type": "trace", "on_missing": "fail"},
        "evaluator": {
            "type": "trace_rule",
            "rule": {"op": "never", "match": {"event": "budget_exceeded"}},
        },
        "scoring": {"type": "binary", "range": [0, 1]},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "provisional": False,
    }
    metric.update(overrides)
    return metric


def judge_metric(**overrides):
    metric = {
        "metric_id": "responsiveness",
        "name": "response quality",
        "type": "judge",
        "target": {
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        "evaluator": {"type": "llm_judge", "rubric_version_id": "rubric_v1"},
        "scoring": {"type": "numeric", "range": [0, 1]},
        "aggregation": {"method": "mean", "on_error": "exclude"},
        "judge_binding": {
            "provider": "anthropic",
            "model": "claude-opus-5",
            "schema_version": "1",
            "rubric_version": "1",
        },
        "provisional": True,  # the shipped default: usable-but-provisional
    }
    metric.update(overrides)
    return metric


def one_metric_spec(metric: dict) -> EvaluationSpec:
    return validate_spec(make_spec(metrics=[metric]))


def tool_result_event(amount, event_id="e1", *, attempt=0, ts=0):
    return ev(
        "tool_result",
        event_id,
        tool="refund_order",
        payload={"output": {"amount": amount}},
        ts=ts,
        attempt=attempt,
    )


# ---------------------------------------------------------------------------
# trace-rule metrics (§9A.5)
# ---------------------------------------------------------------------------


def test_trace_rule_metric_passes_without_violations():
    spec = one_metric_spec(trace_rule_metric())
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.passed is True
    assert score.score == 1.0


def test_trace_rule_metric_fails_with_evidence():
    spec = one_metric_spec(trace_rule_metric())
    score = evaluate_case(
        spec, "c1",
        [ev("llm_call", "e1"), ev("budget_exceeded", "e2")],
        spec.metrics[0],
    )
    assert score.status is CaseStatus.FAIL
    assert score.score == 0.0
    assert "e2" in score.evidence_event_ids


def test_trace_rule_empty_trace_governed_by_on_missing():
    # §9A.5: a fully empty trace is the ONLY case the metric-level on_missing
    # governs — fail | skip | pass, explicitly, never defaulted.
    for on_missing, expected_status in [
        ("fail", CaseStatus.FAIL),
        ("skip", CaseStatus.SKIPPED),
        ("pass", CaseStatus.PASS),
    ]:
        spec = one_metric_spec(trace_rule_metric(target={"type": "trace", "on_missing": on_missing}))
        score = evaluate_case(spec, "c1", [], spec.metrics[0])
        assert score.status is expected_status
        assert score.score == ({CaseStatus.PASS: 1.0, CaseStatus.FAIL: 0.0, CaseStatus.SKIPPED: None}[expected_status])


def test_trace_rule_nonempty_trace_is_scored_by_the_rule_not_on_missing():
    # The §9A.5 G6 fix: `never` with zero matches on a non-empty trace is a
    # PASS scored by the rule — on_missing does NOT fire.
    spec = one_metric_spec(trace_rule_metric(target={"type": "trace", "on_missing": "fail"}))
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert "rule passed" in score.message


def test_trace_rule_error_routes_to_error_status():
    # An anchored operator at the rule root is unevaluable -> ERROR; the
    # aggregation's on_error decides the run-level rollup (§7A.4).
    metric = trace_rule_metric(
        evaluator={
            "type": "trace_rule",
            "rule": {"op": "exists_before", "match": {"event": "llm_call"}},
        }
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])
    assert score.status is CaseStatus.ERROR
    assert score.passed is False


def test_count_metric_condition_scoring():
    metric = trace_rule_metric(
        metric_id="max_two_tool_calls",
        evaluator={
            "type": "trace_rule",
            "rule": {"op": "count", "match": {"event": "tool_call"}},
        },
        scoring={"type": "binary", "range": [0, 1], "condition": "raw_value <= 2"},
    )
    spec = one_metric_spec(metric)
    events = [
        ev("tool_call", "e1", tool="a"),
        ev("tool_call", "e2", tool="b"),
    ]
    score = evaluate_case(spec, "c1", events, spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.score == 1.0
    three = [*events, ev("tool_call", "e3", tool="c")]
    score = evaluate_case(spec, "c1", three, spec.metrics[0])
    assert score.status is CaseStatus.FAIL
    assert score.score == 0.0


def test_count_metric_condition_compound_grammar():
    metric = trace_rule_metric(
        metric_id="between_two_and_three",
        evaluator={
            "type": "trace_rule",
            "rule": {"op": "count", "match": {"event": "tool_call"}},
        },
        scoring={"type": "binary", "range": [0, 1], "condition": "raw_value >= 2 && raw_value <= 3"},
    )
    spec = one_metric_spec(metric)
    events = [ev("tool_call", "e1", tool="a"), ev("tool_call", "e2", tool="b")]
    assert evaluate_case(spec, "c1", events, spec.metrics[0]).status is CaseStatus.PASS
    four = [*events, ev("tool_call", "e3", tool="c"), ev("tool_call", "e4", tool="d")]
    assert evaluate_case(spec, "c1", four, spec.metrics[0]).status is CaseStatus.FAIL


def test_count_metric_condition_compile_error_is_error():
    metric = trace_rule_metric(
        metric_id="bad_condition",
        evaluator={
            "type": "trace_rule",
            "rule": {"op": "count", "match": {"event": "tool_call"}},
        },
        scoring={"type": "binary", "range": [0, 1], "condition": "raw_value =="},
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [ev("tool_call", "e1", tool="a")], spec.metrics[0])
    assert score.status is CaseStatus.ERROR


# ---------------------------------------------------------------------------
# scalar metrics (§7A.3)
# ---------------------------------------------------------------------------


def test_scalar_exact_match_pass_and_fail():
    spec = one_metric_spec(scalar_metric())
    assert evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0]).status is CaseStatus.PASS
    assert evaluate_case(spec, "c1", [tool_result_event(50.0)], spec.metrics[0]).status is CaseStatus.FAIL


def test_scalar_selector_missing_never_passes():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.does_not_exist",
            "occurrence": "last",
            "on_missing": "pass",
        }
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])
    # A MISSING selection never passes — and never errors; it is a FAIL here
    # because the observed value itself did not match.
    assert score.status is CaseStatus.FAIL


def test_scalar_zero_target_events_governed_by_on_missing():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "skip",
        }
    )
    spec = one_metric_spec(metric)
    # The tool name is wrong -> zero matched events -> on_missing decides.
    wrong_tool = ev("tool_result", "e1", tool="other_tool", payload={"output": {"amount": 1}})
    score = evaluate_case(spec, "c1", [wrong_tool], spec.metrics[0])
    assert score.status is CaseStatus.SKIPPED


def test_scalar_occurrence_first_and_last():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "first",
            "on_missing": "fail",
        },
        evaluator={"type": "exact_match", "expected": 10.0},
    )
    spec = one_metric_spec(metric)
    events = [tool_result_event(10.0, "e1", ts=0), tool_result_event(99.0, "e2", ts=100)]
    score = evaluate_case(spec, "c1", events, spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.evidence_event_ids == ("e1",)

    last_metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        evaluator={"type": "exact_match", "expected": 99.0},
    )
    spec = one_metric_spec(last_metric)
    assert evaluate_case(spec, "c1", events, spec.metrics[0]).status is CaseStatus.PASS


def test_scalar_occurrence_all_requires_every_selected_event():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "all",
            "on_missing": "fail",
        },
        evaluator={"type": "exact_match", "expected": 49.99},
    )
    spec = one_metric_spec(metric)
    ok = [tool_result_event(49.99, "e1", ts=0), tool_result_event(49.99, "e2", ts=100)]
    assert evaluate_case(spec, "c1", ok, spec.metrics[0]).status is CaseStatus.PASS
    mixed = [tool_result_event(49.99, "e1", ts=0), tool_result_event(50.0, "e2", ts=100)]
    assert evaluate_case(spec, "c1", mixed, spec.metrics[0]).status is CaseStatus.FAIL


def test_scalar_occurrence_index_out_of_range_behaves_as_on_missing():
    # §7A.3: index:N out of range behaves exactly as on_missing.
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "index:5",
            "on_missing": "pass",
        }
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert "on_missing" in score.message


def test_scalar_numeric_scoring_normalizes():
    metric = scalar_metric(
        evaluator={"type": "numeric", "expected": 50.0, "tolerance": 0},
        scoring={"type": "numeric", "range": [0, 100]},
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [tool_result_event(75.0)], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.score == 0.75
    out_of_range = evaluate_case(spec, "c1", [tool_result_event(150.0)], spec.metrics[0])
    assert out_of_range.status is CaseStatus.FAIL
    assert out_of_range.score == 1.0  # clamped, but never passed


def test_scalar_regex_evaluator():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.note",
            "occurrence": "last",
            "on_missing": "fail",
        },
        evaluator={"type": "regex", "pattern": r"refunded.*order[ ]*#?\d+"},
    )
    spec = one_metric_spec(metric)
    ok = ev("tool_result", "e1", tool="refund_order", payload={"output": {"note": "refunded order #42"}})
    assert evaluate_case(spec, "c1", [ok], spec.metrics[0]).status is CaseStatus.PASS
    bad = ev("tool_result", "e1", tool="refund_order", payload={"output": {"note": "denied"}})
    assert evaluate_case(spec, "c1", [bad], spec.metrics[0]).status is CaseStatus.FAIL


def test_scalar_cel_predicate_evaluator():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "all",
            "on_missing": "fail",
        },
        evaluator={"type": "cel_predicate", "predicate": "payload.output.amount > 50"},
    )
    spec = one_metric_spec(metric)
    events = [tool_result_event(60.0, "e1", ts=0), tool_result_event(70.0, "e2", ts=100)]
    assert evaluate_case(spec, "c1", events, spec.metrics[0]).status is CaseStatus.PASS
    mixed = [tool_result_event(60.0, "e1", ts=0), tool_result_event(10.0, "e2", ts=100)]
    assert evaluate_case(spec, "c1", mixed, spec.metrics[0]).status is CaseStatus.FAIL


def test_scalar_reference_evaluator():
    spec = validate_spec(
        make_spec(
            cases=[
                {
                    "case_id": "c1",
                    "name": "one",
                    "input": {"order_id": "123"},
                    "expected": {"refund_amount": {"tag": "user_stated", "value": 49.99}},
                }
            ],
            metrics=[scalar_metric(evaluator={"type": "reference", "expected": "test.expected.refund_amount"})],
        )
    )
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    score = evaluate_case(spec, "c1", [tool_result_event(50.0)], spec.metrics[0])
    assert score.status is CaseStatus.FAIL


def test_scalar_reference_unresolvable_path_is_error():
    spec = one_metric_spec(scalar_metric(evaluator={"type": "reference", "expected": "test.expected.nope"}))
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])
    assert score.status is CaseStatus.ERROR


def test_scalar_reference_bad_path_is_error():
    spec = one_metric_spec(scalar_metric(evaluator={"type": "reference", "expected": "test.input.x"}))
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])
    assert score.status is CaseStatus.ERROR


def test_scalar_json_schema_evaluator():
    metric = scalar_metric(
        target={
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$",
            "occurrence": "last",
            "on_missing": "fail",
        },
        evaluator={
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"amount": {"type": "number", "minimum": 0}},
                "required": ["amount"],
            },
        },
    )
    spec = one_metric_spec(metric)
    ok = ev("tool_result", "e1", tool="refund_order", payload={"output": {"amount": 49.99}})
    assert evaluate_case(spec, "c1", [ok], spec.metrics[0]).status is CaseStatus.PASS
    missing_key = ev("tool_result", "e1", tool="refund_order", payload={"output": {}})
    assert evaluate_case(spec, "c1", [missing_key], spec.metrics[0]).status is CaseStatus.FAIL
    negative = ev("tool_result", "e1", tool="refund_order", payload={"output": {"amount": -5}})
    assert evaluate_case(spec, "c1", [negative], spec.metrics[0]).status is CaseStatus.FAIL


def test_scalar_input_target():
    metric = scalar_metric(
        target={"type": "input", "selector": "$.order_id", "on_missing": "fail"},
        evaluator={"type": "exact_match", "expected": "123"},
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [], spec.metrics[0])  # no events needed
    assert score.status is CaseStatus.PASS


def test_scalar_non_event_target_is_on_missing():
    # final_response lives outside the trace (§9A.1) — the trace-only
    # evaluator matches zero events and on_missing decides.
    metric = scalar_metric(
        target={"type": "final_response", "selector": "$.text", "on_missing": "fail"}
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])
    assert score.status is CaseStatus.FAIL
    assert "on_missing" in score.message


def test_scalar_run_target_is_rejected():
    metric = scalar_metric(
        target={"type": "run", "on_missing": "fail"},
        evaluator={"type": "exact_match", "expected": 1},
    )
    spec = one_metric_spec(metric)
    with pytest.raises(ValueError):
        evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])


def test_scalar_trace_target_is_error():
    metric = scalar_metric(
        target={"type": "trace", "on_missing": "fail"},
        evaluator={"type": "exact_match", "expected": 1},
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0])
    assert score.status is CaseStatus.ERROR


def test_scalar_categorical_scoring():
    metric = scalar_metric(
        evaluator={"type": "exact_match", "expected": "high"},
        scoring={"type": "categorical", "categories": ["low", "medium", "high"]},
    )
    spec = one_metric_spec(metric)
    score = evaluate_case(spec, "c1", [tool_result_event("high")], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.score == 1.0  # index 2 of 3 -> 2/2
    score = evaluate_case(spec, "c1", [tool_result_event("medium")], spec.metrics[0])
    assert score.status is CaseStatus.PASS
    assert score.score == 0.5
    score = evaluate_case(spec, "c1", [tool_result_event("nope")], spec.metrics[0])
    assert score.status is CaseStatus.FAIL
    assert score.score == 0.0


# ---------------------------------------------------------------------------
# judge metrics (§15A)
# ---------------------------------------------------------------------------


def test_judge_metric_dispatches_to_judge_evaluator():
    spec = one_metric_spec(judge_metric())
    judge = ScriptedJudge(BINDING, scores={"c1": 0.9})
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0], judge=judge)
    assert score.status is CaseStatus.PASS
    assert score.score == 0.9
    assert "e1" in score.evidence_event_ids  # the judge's evidence refs + matched events


def test_judge_metric_without_judge_raises():
    spec = one_metric_spec(judge_metric())
    with pytest.raises(ValueError):
        evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0])


def test_judge_out_of_scale_score_is_error_never_coerced():
    # §15A.2: an out-of-scale verdict is ERROR, never coerced into range.
    spec = one_metric_spec(judge_metric())
    judge = ScriptedJudge(BINDING, scores={"c1": 5.0})
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0], judge=judge)
    assert score.status is CaseStatus.ERROR


def test_judge_zero_target_events_is_on_missing():
    spec = one_metric_spec(judge_metric(target={"type": "final_response", "selector": "$.text", "on_missing": "fail"}))
    judge = ScriptedJudge(BINDING, scores={"c1": 0.9})
    score = evaluate_case(spec, "c1", [ev("llm_call", "e1")], spec.metrics[0], judge=judge)
    assert score.status is CaseStatus.FAIL
    assert "on_missing" in score.message


def test_judge_scores_are_provisional_by_default():
    spec = one_metric_spec(judge_metric())
    judge = ScriptedJudge(BINDING, scores={"c1": 0.9})
    score = evaluate_case(spec, "c1", [tool_result_event(49.99)], spec.metrics[0], judge=judge)
    assert score.provisional is True


# ---------------------------------------------------------------------------
# first-attempt authority (§11C.3)
# ---------------------------------------------------------------------------


def test_first_attempt_is_authoritative():
    # attempt 0 fails, attempt 1 passes — the case FAILS. A case that passes
    # only on retry never passes (§11C.3: retries mask variance).
    spec = one_metric_spec(trace_rule_metric())
    events = [
        ev("budget_exceeded", "e1", attempt=0),
        ev("llm_call", "e2", attempt=1),
    ]
    score = evaluate_case(spec, "c1", events, spec.metrics[0])
    assert score.status is CaseStatus.FAIL


def test_retry_violations_are_ignored_when_first_attempt_passes():
    spec = one_metric_spec(trace_rule_metric())
    events = [
        ev("llm_call", "e1", attempt=0),
        ev("budget_exceeded", "e2", attempt=1),
    ]
    score = evaluate_case(spec, "c1", events, spec.metrics[0])
    assert score.status is CaseStatus.PASS


def test_score_attempt_flags_retry_override():
    spec = one_metric_spec(trace_rule_metric())
    events = [ev("llm_call", "e1", attempt=1)]
    score = score_attempt(spec, "c1", events, spec.metrics[0])
    assert score.on_retry_override is True
    assert score.attempt == 1
    events0 = [ev("llm_call", "e1", attempt=0)]
    score0 = score_attempt(spec, "c1", events0, spec.metrics[0])
    assert score0.on_retry_override is False


def test_score_attempt_rejects_mixed_attempts():
    spec = one_metric_spec(trace_rule_metric())
    events = [ev("llm_call", "e1", attempt=0), ev("llm_call", "e2", attempt=1)]
    with pytest.raises(ValueError):
        score_attempt(spec, "c1", events, spec.metrics[0])


def test_retry_override_never_enters_pass_rate():
    spec = one_metric_spec(trace_rule_metric())
    retry_score = score_attempt(spec, "c1", [ev("llm_call", "e1", attempt=1)], spec.metrics[0])
    aggregate = aggregate_metric(spec, spec.metrics[0], [retry_score])
    # The retry result is dropped: no denominator, no fake complete numbers.
    assert aggregate.n_cases == 0
    assert aggregate.pass_rate is None
    assert aggregate.value is None


# ---------------------------------------------------------------------------
# aggregation (§7A.2/§13A)
# ---------------------------------------------------------------------------


def pass_fail_scores(metric, statuses):
    scores = []
    for i, status in enumerate(statuses):
        if status is CaseStatus.PASS:
            scores.append(CaseScore(metric.metric_id, f"c{i}", 1.0, True, (), status, "ok"))
        else:
            scores.append(CaseScore(metric.metric_id, f"c{i}", 0.0, False, (), status, "no"))
    return scores


def test_aggregation_pass_rate():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    scores = pass_fail_scores(metric, [CaseStatus.PASS, CaseStatus.FAIL])
    agg = aggregate_metric(spec, metric, scores)
    assert agg.pass_rate == 0.5
    assert agg.value == 0.5
    assert agg.n_cases == 2
    assert agg.n_denominator == 2
    assert agg.aggregation_state is AggregationState.COMPLETE


def test_aggregation_partial_when_cases_missing():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    agg = aggregate_metric(spec, metric, [CaseScore(metric.metric_id, "c1", 1.0, True, (), CaseStatus.PASS, "ok")])
    assert agg.aggregation_state is AggregationState.PARTIAL
    assert agg.pass_rate == 1.0  # partial numbers, honest state


def test_aggregation_in_progress():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    scores = pass_fail_scores(metric, [CaseStatus.PASS])
    agg = aggregate_metric(spec, metric, scores, in_progress=True)
    assert agg.aggregation_state is AggregationState.IN_PROGRESS


def test_aggregation_complete_with_expected_n():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    scores = pass_fail_scores(metric, [CaseStatus.PASS])
    agg = aggregate_metric(spec, metric, scores, expected_n=1)
    assert agg.aggregation_state is AggregationState.COMPLETE


def test_aggregation_on_error_fail_counts_error_as_failure():
    spec = one_metric_spec(trace_rule_metric(aggregation={"method": "pass_rate", "on_error": "fail"}))
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", 1.0, True, (), CaseStatus.PASS, "ok"),
        CaseScore(metric.metric_id, "c2", None, False, (), CaseStatus.ERROR, "boom"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    assert agg.error_n == 1
    assert agg.pass_rate == 0.5  # ERROR counts as a failure, in the denominator
    assert agg.n_denominator == 2


def test_aggregation_on_error_exclude_drops_error():
    spec = one_metric_spec(trace_rule_metric(aggregation={"method": "pass_rate", "on_error": "exclude"}))
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", 1.0, True, (), CaseStatus.PASS, "ok"),
        CaseScore(metric.metric_id, "c2", None, False, (), CaseStatus.ERROR, "boom"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    assert agg.error_n == 1
    assert agg.pass_rate == 1.0
    assert agg.n_denominator == 1


def test_aggregation_skipped_excluded_from_denominator():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", 1.0, True, (), CaseStatus.PASS, "ok"),
        CaseScore(metric.metric_id, "c2", None, False, (), CaseStatus.SKIPPED, "skip"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    assert agg.skipped_n == 1
    assert agg.pass_rate == 1.0
    assert agg.n_denominator == 1


def test_aggregation_empty_denominator_is_none():
    spec = one_metric_spec(trace_rule_metric(aggregation={"method": "pass_rate", "on_error": "exclude"}))
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", None, False, (), CaseStatus.ERROR, "boom"),
        CaseScore(metric.metric_id, "c2", None, False, (), CaseStatus.ERROR, "boom"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    assert agg.pass_rate is None
    assert agg.value is None
    assert agg.mean is None
    assert agg.error_n == 2


def test_aggregation_rejects_mismatched_metric_id():
    spec = one_metric_spec(trace_rule_metric())
    metric = spec.metrics[0]
    other = CaseScore("other_metric", "c1", 1.0, True, (), CaseStatus.PASS, "ok")
    with pytest.raises(ValueError):
        aggregate_metric(spec, metric, [other])


def test_aggregation_p50_nearest_rank():
    spec = one_metric_spec(trace_rule_metric(aggregation={"method": "p50", "on_error": "exclude"}))
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", 0.2, True, (), CaseStatus.PASS, "ok"),
        CaseScore(metric.metric_id, "c2", 0.8, True, (), CaseStatus.PASS, "ok"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    # Nearest-rank p50: rank = ceil(0.5 * 2) = 1 -> the 1st smallest = 0.2.
    assert agg.value == 0.2
    assert agg.pass_rate == 1.0


def test_aggregation_mean_and_sum():
    spec = one_metric_spec(trace_rule_metric(aggregation={"method": "mean", "on_error": "exclude"}))
    metric = spec.metrics[0]
    scores = [
        CaseScore(metric.metric_id, "c1", 0.25, True, (), CaseStatus.PASS, "ok"),
        CaseScore(metric.metric_id, "c2", 0.75, True, (), CaseStatus.PASS, "ok"),
    ]
    agg = aggregate_metric(spec, metric, scores)
    assert agg.value == 0.5
    assert agg.mean == 0.5


# ---------------------------------------------------------------------------
# the gate (§7A.2/§11C) and gate_safe (§15A.1)
# ---------------------------------------------------------------------------


def test_gate_pass_and_fail():
    spec = one_metric_spec(trace_rule_metric(gate={"min": 1.0}))
    metric = spec.metrics[0]
    scores = pass_fail_scores(metric, [CaseStatus.PASS, CaseStatus.PASS])
    agg = aggregate_metric(spec, metric, scores)
    result = evaluate_gate(metric, agg)
    assert result.active is True
    assert result.passed is True
    fail_scores = pass_fail_scores(metric, [CaseStatus.PASS, CaseStatus.FAIL])
    result = evaluate_gate(metric, aggregate_metric(spec, metric, fail_scores))
    assert result.passed is False


def test_gate_without_declared_gate_is_inactive():
    spec = one_metric_spec(trace_rule_metric(gate=None))
    metric = spec.metrics[0]
    scores = pass_fail_scores(metric, [CaseStatus.FAIL])
    result = evaluate_gate(metric, aggregate_metric(spec, metric, scores))
    assert result.active is False
    assert result.passed is True  # reports but never fails a run


def test_gate_with_no_value_fails():
    spec = one_metric_spec(trace_rule_metric(gate={"min": 1.0}))
    metric = spec.metrics[0]
    agg = RunAggregate(
        metric.metric_id, None, None, None, 0, 0,
        aggregation_state=AggregationState.COMPLETE,
    )
    result = evaluate_gate(metric, agg)
    assert result.active is True
    assert result.passed is False


def test_gate_safe_excludes_provisional_metrics():
    metric = trace_rule_metric(provisional=True)
    spec = one_metric_spec(metric)
    assert gate_safe(spec.metrics, readiness=None) == []


def test_gate_safe_judge_requires_calibrated_binding():
    spec = one_metric_spec(judge_metric(provisional=False))
    metric = spec.metrics[0]
    assert gate_safe(spec.metrics, readiness=None) == []
    registry = JudgeReadinessRegistry()
    assert gate_safe(spec.metrics, readiness=registry) == []  # UNCALIBRATED
    registry.set(
        JudgeReadiness(binding=BINDING).with_label(30).with_kappa(0.7)
    )
    assert gate_safe(spec.metrics, readiness=registry) == [metric]


def test_gate_safe_deterministic_metrics_included():
    spec = one_metric_spec(trace_rule_metric(provisional=False))
    assert gate_safe(spec.metrics) == [spec.metrics[0]]


def test_gate_safe_judge_metric_without_binding_never_safe():
    # Validation rejects a judge metric without a binding (§15A.5) — the
    # gate_safe branch is defensive against unvalidated (construct-escaped)
    # metrics; reach it via model_construct.
    from llm_agent_eval.spec import Metric

    validated = one_metric_spec(judge_metric(provisional=False))
    fields = validated.metrics[0].model_dump()
    fields["judge_binding"] = None
    fields["provisional"] = False
    metric = Metric.model_construct(**fields)
    registry = JudgeReadinessRegistry()
    registry.set(JudgeReadiness(binding=BINDING).with_label(30).with_kappa(0.7))
    assert gate_safe([metric], readiness=registry) == []


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_unknown_case_id_raises():
    spec = one_metric_spec(trace_rule_metric())
    with pytest.raises(ValueError):
        evaluate_case(spec, "nope", [ev("llm_call", "e1")], spec.metrics[0])


def test_cross_case_contamination_rejected():
    spec = one_metric_spec(trace_rule_metric())
    bad = make_event(
        event_type="llm_call",
        run_id="run1",
        case_id="c2",  # not the case being scored
        workspace_id="ws1",
        attempt_id="a0",
        event_id="e9",
        timestamp=T0,
    )
    with pytest.raises(ValueError):
        evaluate_case(spec, "c1", [bad], spec.metrics[0])


def test_non_trace_event_rejected():
    spec = one_metric_spec(trace_rule_metric())
    with pytest.raises(TypeError):
        evaluate_case(spec, "c1", ["not-an-event"], spec.metrics[0])
