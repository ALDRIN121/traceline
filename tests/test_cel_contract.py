"""CEL contract: v3 environment, missing/error policy, and evaluator rollup."""

from datetime import datetime, timedelta, timezone

import pytest

from llm_agent_eval.evaluators import CaseScore, CaseStatus, aggregate_metric, evaluate_case
from llm_agent_eval.events import make_event
from llm_agent_eval.predicates import Predicate, PredicateSemanticError
from llm_agent_eval.spec import validate_spec
from llm_agent_eval.trace_rules import evaluate_rule

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def evt(**fields):
    return make_event(
        event_type=fields.pop("event_type", "llm_response"),
        run_id="run_1",
        case_id="case_1",
        workspace_id="ws_1",
        attempt_id="att_1",
        **fields,
    )


def test_cel_map_index_compiles_and_evaluates():
    pred = Predicate.compile("payload['amount'] == 49.99", language_version="cel")
    assert pred.language_version == "cel"
    assert pred.evaluate(evt(payload={"amount": 49.99})) is True
    assert pred.evaluate(evt(payload={"amount": 1})) is False


def test_unknown_identifier_fails_at_validation():
    with pytest.raises(PredicateSemanticError):
        Predicate.compile("not_a_declared_variable == 1", language_version="cel")


def test_missing_required_target_uses_configured_on_missing():
    spec = validate_spec({
        "spec_version": "1",
        "name": "missing-target",
        "dataset_version": "v1",
        "metrics": [{
            "metric_id": "m1",
            "name": "output present",
            "type": "scalar",
            "evaluator": {
                "type": "cel_predicate",
                "predicate": "payload['ok'] == true",
                "language_version": "cel",
            },
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0},
            "target": {"type": "tool_output", "tool": "lookup", "on_missing": "fail"},
        }],
        "cases": [{"case_id": "c1", "name": "one", "input": {"q": "x"}}],
    })
    score = evaluate_case(spec, "c1", [], spec.metrics[0])
    assert score.status is CaseStatus.FAIL
    assert "on_missing" in score.message


def test_for_all_includes_adapter_tool_events():
    events = [
        make_event(
            event_type="tool_call", event_id="adapter-call", run_id="r", case_id="c",
            workspace_id="w", attempt_id="a", tool="refund_order", source="adapter",
            timestamp=T0, sequence=1, payload={"order_id": "1"},
        ),
        make_event(
            event_type="tool_result", event_id="adapter-check", run_id="r", case_id="c",
            workspace_id="w", attempt_id="a", tool="check_refund_eligibility", source="adapter",
            timestamp=T0 - timedelta(milliseconds=5), sequence=0,
            payload={"output": {"eligible": True}},
        ),
    ]
    result = evaluate_rule({
        "op": "for_all",
        "match": {"event": "tool_call", "tool": "refund_order"},
        "assert": {
            "op": "exists_before",
            "match": {
                "event": "tool_result",
                "tool": "check_refund_eligibility",
                "where": "payload['output']['eligible'] == true",
            },
        },
    }, events)
    assert result.result is True
    assert "adapter-call" in result.matched


def test_fail_fail_policy_counts_error_and_missing_in_denominator():
    scores = [
        *[CaseScore("m", f"p{i}", 1.0, True, (), CaseStatus.PASS, "ok") for i in range(16)],
        CaseScore("m", "f0", 0.0, False, (), CaseStatus.FAIL, "fail"),
        CaseScore("m", "f1", 0.0, False, (), CaseStatus.FAIL, "fail"),
        CaseScore("m", "e0", None, False, (), CaseStatus.ERROR, "error"),
        CaseScore("m", "m0", 0.0, False, (), CaseStatus.FAIL, "on_missing=fail"),
    ]
    spec = validate_spec({
        "spec_version": "1",
        "name": "rollup",
        "dataset_version": "v1",
        "metrics": [{
            "metric_id": "m",
            "name": "rate",
            "type": "scalar",
            "evaluator": {"type": "exact_match", "expected": True},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 0.8},
            "target": {"type": "final_response", "on_missing": "fail"},
        }],
        "cases": [{"case_id": "c1", "name": "one", "input": {}}],
    })
    agg = aggregate_metric(spec, spec.metrics[0], scores, expected_n=20)
    assert agg.value == pytest.approx(0.8)
    assert agg.error_n == 1
