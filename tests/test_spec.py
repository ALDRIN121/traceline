"""Tests for the validated evaluation models (src/llm_agent_eval/spec.py).

Covers the §7A validation rules: on_missing/on_error are required with no
defaults, judge metrics require judge_binding, cases require an id, ids are
unique, and the closed enums / per-type configs are enforced. SpecValidationError
carries a structured (field, message) problem list for the harness repair loop.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_agent_eval.spec import (
    EvaluationSpec,
    SpecValidationError,
    validate_spec,
)


def make_spec(**overrides):
    """A fully valid spec: a scalar metric on a tool_output target."""
    spec = {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "cases": [
            {
                "case_id": "c1",
                "name": "eligible refund",
                "description": "refund for an eligible order",
                "input": {"order_id": "123"},
                "expected": {
                    "refund_amount": {"tag": "user_stated", "value": 49.99},
                },
            }
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
            }
        ],
    }
    spec.update(overrides)
    return spec


def make_judge_metric(**overrides):
    metric = {
        "metric_id": "responsiveness",
        "name": "response quality",
        "type": "judge",
        "target": {"type": "final_response", "selector": "$.text", "on_missing": "fail"},
        "evaluator": {"type": "llm_judge", "rubric_version_id": "rubric_v1"},
        "scoring": {"type": "numeric", "range": [0, 1]},
        "aggregation": {"method": "mean", "on_error": "exclude"},
        "judge_binding": {
            "provider": "anthropic",
            "model": "claude-opus-5",
            "schema_version": "1",
            "rubric_version": "1",
        },
    }
    metric.update(overrides)
    return metric


def make_trace_rule_metric(**overrides):
    metric = {
        "metric_id": "never_budget_exceeded",
        "name": "no budget abort",
        "type": "trace_rule",
        "target": {"type": "trace", "on_missing": "pass"},
        "evaluator": {
            "type": "trace_rule",
            "rule": {"op": "never", "match": {"event": "budget_exceeded"}},
        },
        "scoring": {"type": "binary", "range": [0, 1]},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "gate": {"min": 1.0},
    }
    metric.update(overrides)
    return metric


def problems(spec_dict) -> list[tuple[str, str]]:
    with pytest.raises(SpecValidationError) as excinfo:
        validate_spec(spec_dict)
    return [(p.field, p.message) for p in excinfo.value.problems]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_valid_spec_validates():
    spec = validate_spec(make_spec())
    assert isinstance(spec, EvaluationSpec)
    assert spec.name == "refund-suite"
    assert spec.spec_version == "1"
    assert spec.dataset_version == "v1"
    assert spec.run_tier is None
    assert spec.cases[0].expected["refund_amount"].tag == "user_stated"
    assert spec.metrics[0].weight == 1.0
    assert spec.metrics[0].gate.min == 1.0  # Gate is a model, not a dict


def test_valid_spec_accepts_json_text():
    import json

    spec = validate_spec(json.dumps(make_spec()))
    assert len(spec.cases) == 1
    assert spec.metrics[0].metric_id == "refund_amount"


def test_run_tier_hint_accepted_and_validated():
    assert validate_spec(make_spec(run_tier="quick")).run_tier == "quick"
    assert validate_spec(make_spec(run_tier="full")).run_tier == "full"
    (field, message), = problems(make_spec(run_tier="huge"))
    assert "run_tier" in field or "run_tier" in message


def test_canonical_safety_rule_validates():
    """§9A.5's canonical refund-order rule — every refund_order tool_call must
    be preceded by an eligibility check whose output was eligible."""
    spec = make_spec()
    spec["metrics"] = [
        {
            **make_trace_rule_metric(),
            "metric_id": "eligible_before_refund",
            "evaluator": {
                "type": "trace_rule",
                "rule": {
                    "op": "for_all",
                    "match": {"event": "tool_call", "tool": "refund_order"},
                    "assert": {
                        "op": "exists_before",
                        "match": {
                            "event": "tool_result",
                            "tool": "check_refund_eligibility",
                            "where": "payload.output.eligible == true",
                        },
                    },
                },
            },
        }
    ]
    assert validate_spec(spec).metrics[0].metric_id == "eligible_before_refund"


def test_judge_metric_with_binding_validates_and_is_provisional_by_default():
    spec = make_spec(metrics=[make_judge_metric()])
    metric = validate_spec(spec).metrics[0]
    assert metric.judge_binding.provider == "anthropic"
    assert metric.provisional is True  # UNCALIBRATED is the shipped default (4J)
    # An explicitly calibrated judge can clear the flag.
    metric2 = validate_spec(make_spec(metrics=[make_judge_metric(provisional=False)])).metrics[0]
    assert metric2.provisional is False


def test_run_target_does_not_require_on_missing():
    """§7A.5: on_missing is not applicable to run targets."""
    spec = make_spec()
    spec["metrics"][0]["target"] = {
        "type": "run",
        "selector": "$.latency_p95",
    }
    spec["metrics"][0]["aggregation"] = {"method": "p95", "on_error": "fail"}
    assert validate_spec(spec).metrics[0].target.type == "run"


# ---------------------------------------------------------------------------
# The on_missing / on_error invariant (no defaults)
# ---------------------------------------------------------------------------


def test_metric_missing_on_missing_is_rejected():
    spec = make_spec()
    del spec["metrics"][0]["target"]["on_missing"]
    (field, message), = problems(spec)
    assert "on_missing" in field or "on_missing" in message
    assert "no default" in message or "required" in message


def test_metric_missing_on_error_is_rejected():
    spec = make_spec()
    del spec["metrics"][0]["aggregation"]["on_error"]
    (field, message), = problems(spec)
    assert "on_error" in field or "on_error" in message


def test_on_missing_value_must_be_closed_enum():
    spec = make_spec()
    spec["metrics"][0]["target"]["on_missing"] = "maybe"
    (field, message), = problems(spec)
    assert "on_missing" in field or "on_missing" in message


def test_on_error_value_must_be_fail_or_exclude():
    spec = make_spec()
    spec["metrics"][0]["aggregation"]["on_error"] = "warn"
    (field, message), = problems(spec)
    assert "on_error" in field or "on_error" in message


# ---------------------------------------------------------------------------
# Judge metric contract
# ---------------------------------------------------------------------------


def test_judge_metric_without_binding_is_rejected():
    spec = make_spec(metrics=[make_judge_metric(judge_binding=None)])
    (field, message), = problems(spec)
    assert "judge_binding" in message


def test_judge_type_requires_llm_judge_evaluator():
    spec = make_spec(metrics=[make_judge_metric()])
    spec["metrics"][0]["evaluator"] = {"type": "numeric", "expected": 5, "tolerance": 0}
    (field, message), = problems(spec)
    assert "llm_judge" in message


# ---------------------------------------------------------------------------
# Case identity
# ---------------------------------------------------------------------------


def test_case_without_id_is_rejected():
    spec = make_spec()
    del spec["cases"][0]["case_id"]
    (field, message), = problems(spec)
    assert "case_id" in field or "case_id" in message
    # An empty id is "without an id" too.
    spec = make_spec()
    spec["cases"][0]["case_id"] = ""
    (field, message), = problems(spec)
    assert "case_id" in field or "case_id" in message


def test_duplicate_case_ids_are_rejected():
    spec = make_spec()
    spec["cases"].append({**spec["cases"][0]})
    (field, message), = problems(spec)
    assert "duplicate case_id" in message and "c1" in message


def test_duplicate_metric_ids_are_rejected():
    spec = make_spec()
    spec["metrics"].append({**spec["metrics"][0]})
    (field, message), = problems(spec)
    assert "duplicate metric_id" in message


# ---------------------------------------------------------------------------
# Target (§7A.3)
# ---------------------------------------------------------------------------


def test_tool_scoped_target_requires_tool():
    spec = make_spec()
    del spec["metrics"][0]["target"]["tool"]
    (field, message), = problems(spec)
    assert "tool" in message


def test_occurrence_shape_is_closed():
    for bad in ("banana", "index:", "Index:2", "index:two"):
        spec = make_spec()
        spec["metrics"][0]["target"]["occurrence"] = bad
        (field, message), = problems(spec)
        assert "occurrence" in message, bad
    for good in ("first", "last", "all", "index:0", "index:7"):
        spec = make_spec()
        spec["metrics"][0]["target"]["occurrence"] = good
        assert validate_spec(spec).metrics[0].target.occurrence == good


def test_selector_must_start_with_dollar():
    spec = make_spec()
    spec["metrics"][0]["target"]["selector"] = "amount"
    (field, message), = problems(spec)
    assert "selector" in message


def test_target_type_is_closed():
    spec = make_spec()
    spec["metrics"][0]["target"]["type"] = "tool_outputs"
    (field, message), = problems(spec)
    assert "target" in field or "type" in message


# ---------------------------------------------------------------------------
# Evaluator (§7A.2, per-type config)
# ---------------------------------------------------------------------------


def test_numeric_evaluator_requires_expected_and_tolerance():
    spec = make_spec()
    del spec["metrics"][0]["evaluator"]["tolerance"]
    (field, message), = problems(spec)
    assert "tolerance" in message
    spec = make_spec()
    spec["metrics"][0]["evaluator"]["expected"] = "49.99"
    (field, message), = problems(spec)
    assert "expected" in message


def test_cel_predicate_requires_nonempty_predicate():
    spec = make_spec()
    spec["metrics"][0] = {
        **make_trace_rule_metric(),
        "type": "scalar",
        "evaluator": {"type": "cel_predicate", "predicate": ""},
        "target": {"type": "final_response", "selector": "$.text", "on_missing": "fail"},
    }
    (field, message), = problems(spec)
    assert "predicate" in message


def test_llm_judge_requires_rubric_version_reference():
    spec = make_spec(metrics=[make_judge_metric()])
    del spec["metrics"][0]["evaluator"]["rubric_version_id"]
    (field, message), = problems(spec)
    assert "rubric_version_id" in message


def test_unknown_evaluator_type_is_rejected():
    spec = make_spec()
    spec["metrics"][0]["evaluator"] = {"type": "regex_2026", "pattern": "."}
    (field, message), = problems(spec)
    assert "evaluator" in field or "regex_2026" in message


def test_unknown_metric_field_is_rejected():
    """§7A.6: rejected, never silently adjusted."""
    spec = make_spec()
    spec["metrics"][0]["threshold"] = 0.5
    (field, message), = problems(spec)
    assert "threshold" in field or "threshold" in message


# ---------------------------------------------------------------------------
# Trace-rule body validation (§9A.3)
# ---------------------------------------------------------------------------


def test_trace_rule_requires_match_event_from_closed_types():
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {"op": "never", "match": {"event": "final_response"}}
    (field, message), = problems(spec)
    assert "event" in message  # final_response is not a trace event (§9A.1)


def test_trace_rule_unknown_op_is_rejected():
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {"op": "every", "match": {"event": "tool_call"}}
    (field, message), = problems(spec)
    assert "op" in message


def test_count_is_terminal_root_only():
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {
        "op": "and",
        "rules": [
            {"op": "count", "match": {"event": "tool_call", "tool": "refund_order"}},
            {"op": "never", "match": {"event": "budget_exceeded"}},
        ],
    }
    (field, message), = problems(spec)
    assert "terminal" in message
    # As the root op it is fine (with binary scoring it needs a condition).
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {
        "op": "count",
        "match": {"event": "tool_call", "tool": "refund_order"},
    }
    (field, message), = problems(spec)
    assert "condition" in message  # binary scoring of count requires scoring.condition


def test_for_all_requires_assert():
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {"op": "for_all", "match": {"event": "tool_call"}}
    (field, message), = problems(spec)
    assert "assert" in message


def test_match_source_is_closed():
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {
        "op": "never",
        "match": {"event": "error", "source": "shim"},
    }
    (field, message), = problems(spec)
    assert "source" in message
    # Explicit proxy|adapter|runner selection is allowed (§9A.6 opt-in).
    spec = make_spec(metrics=[make_trace_rule_metric()])
    spec["metrics"][0]["evaluator"]["rule"] = {
        "op": "never",
        "match": {"event": "error", "source": "adapter"},
    }
    assert validate_spec(spec).metrics[0].metric_id == "never_budget_exceeded"


# ---------------------------------------------------------------------------
# Scoring / Aggregation / Gate (§7A.2)
# ---------------------------------------------------------------------------


def test_binary_and_numeric_scoring_require_range():
    for scoring in ({"type": "binary"}, {"type": "numeric"}):
        spec = make_spec()
        spec["metrics"][0]["scoring"] = scoring
        (field, message), = problems(spec)
        assert "range" in message


def test_categorical_scoring_requires_categories():
    spec = make_spec()
    spec["metrics"][0]["scoring"] = {"type": "categorical"}
    (field, message), = problems(spec)
    assert "categories" in message


def test_binary_scoring_of_numeric_producing_evaluator_requires_condition():
    spec = make_spec()
    del spec["metrics"][0]["scoring"]["condition"]
    (field, message), = problems(spec)
    assert "condition" in message


def test_aggregation_method_is_closed():
    spec = make_spec()
    spec["metrics"][0]["aggregation"]["method"] = "average"
    (field, message), = problems(spec)
    assert "method" in message or "aggregation" in field


def test_gate_requires_min_or_max_and_is_optional():
    spec = make_spec()
    spec["metrics"][0]["gate"] = {}
    (field, message), = problems(spec)
    assert "gate" in message
    spec = make_spec()
    spec["metrics"][0]["gate"] = None  # absent ⇒ reports but never fails a run
    assert validate_spec(spec).metrics[0].gate is None
    spec = make_spec()
    spec["metrics"][0]["gate"] = {"min": 0.9, "max": 0.8}
    (field, message), = problems(spec)
    assert "min" in message and "max" in message


# ---------------------------------------------------------------------------
# TestCase: expected tags and U2 script
# ---------------------------------------------------------------------------


def test_expected_value_tags_are_closed():
    spec = make_spec()
    spec["cases"][0]["expected"]["x"] = {"tag": "inferred", "value": 5}
    assert validate_spec(spec).cases[0].expected["x"].tag == "inferred"
    spec["cases"][0]["expected"]["y"] = {"tag": "llm_hallucinated", "value": 5}
    (field, message), = problems(spec)
    assert field == "cases[0].expected.y.tag"  # the literal is closed
    assert "user_stated" in message and "derived_from_trace" in message


def test_expected_value_requires_tag_wrapper():
    spec = make_spec()
    spec["cases"][0]["expected"]["refund_amount"] = 49.99  # bare value, no tag
    (field, message), = problems(spec)
    assert "expected" in field


def test_script_steps_are_typed():
    spec = make_spec()
    spec["cases"][0]["script"] = [
        {"kind": "wait_for_input", "requested_input": "Approve this refund?"},
        {"kind": "user_response", "provided_input": "yes"},
    ]
    case = validate_spec(spec).cases[0]
    assert [s.kind for s in case.script] == ["wait_for_input", "user_response"]
    assert case.script[0].requested_input == "Approve this refund?"
    assert case.script[1].source == "simulator"  # U1 default
    # A user_response step must not carry requested_input.
    spec = make_spec()
    spec["cases"][0]["script"] = [
        {"kind": "user_response", "requested_input": "nope", "provided_input": "yes"}
    ]
    (field, message), = problems(spec)
    assert "user_response" in message or "requested_input" in message


def test_case_without_script_defaults_empty():
    spec = make_spec()
    assert validate_spec(spec).cases[0].script == []


# ---------------------------------------------------------------------------
# validate_spec / SpecValidationError structure (the repair loop contract)
# ---------------------------------------------------------------------------


def test_validation_error_carries_structured_problems():
    spec = make_spec()
    del spec["metrics"][0]["target"]["on_missing"]
    del spec["metrics"][0]["aggregation"]["on_error"]
    del spec["cases"][0]["case_id"]
    with pytest.raises(SpecValidationError) as excinfo:
        validate_spec(spec)
    err = excinfo.value
    assert isinstance(err.problems, list)
    assert len(err.problems) >= 3
    for problem in err.problems:
        assert problem.field and problem.message
    assert "metrics[0].target" in {p.field for p in err.problems}  # the Target model rejected it
    assert "metrics[0].aggregation.on_error" in {p.field for p in err.problems}
    assert "cases[0].case_id" in {p.field for p in err.problems}
    assert "on_missing" in str(err)


def test_validation_error_does_not_raise_plain_validation_error():
    with pytest.raises(SpecValidationError):
        validate_spec({"not": "a spec"})


def test_valid_spec_does_not_raise():
    spec = validate_spec(make_spec())
    assert spec.spec_version == "1"
