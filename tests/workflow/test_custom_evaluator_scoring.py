from __future__ import annotations

from llm_agent_eval.events import make_event
from llm_agent_eval.evaluators import CaseStatus, score_custom
from llm_agent_eval.spec import validate_spec


def _spec():
    return validate_spec({
        "spec_version": "1",
        "name": "custom",
        "dataset_version": "d1",
        "cases": [{"case_id": "c1", "name": "one", "input": {"q": "refund"},
                   "expected": {"allowed": {"value": True, "tag": "user_stated"}}}],
        "metrics": [{
            "metric_id": "policy", "name": "policy", "type": "scalar",
            "target": {"type": "trace", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": True},
            "scoring": {"type": "numeric", "range": [0, 1]},
            "aggregation": {"method": "mean", "on_error": "fail"},
        }],
    })


def test_custom_score_receives_redacted_selected_evidence_and_reference():
    calls = []

    def evaluate(evidence, reference):
        calls.append((evidence, reference))
        return {"score": 0.8, "evidence_refs": ["evt-1"], "justification": "allowed"}

    event = make_event(
        event_type="llm_response", run_id="r1", case_id="c1", workspace_id="ws-a",
        attempt_id="a1", event_id="evt-1", payload={"content": "ok"},
    )
    score = score_custom(
        _spec(), "c1", [event], _spec().metrics[0],
        evaluate, evaluator_version="ev-1", pass_threshold=0.5,
        evidence_event_ids=("evt-1",), attempt=0,
    )
    assert score.status is CaseStatus.PASS
    assert score.score == 0.8
    assert score.evaluator_version == "ev-1"
    assert score.evidence_event_ids == ("evt-1",)
    assert calls[0][1]["expected"]["allowed"] is True


def test_custom_score_converts_runtime_failure_to_evaluator_error():
    def evaluate(_evidence, _reference):
        raise ValueError("evaluator_runtime_failed")

    event = make_event(
        event_type="llm_response", run_id="r1", case_id="c1", workspace_id="ws-a",
        attempt_id="a1", event_id="evt-1", payload={"content": "ok"},
    )
    score = score_custom(
        _spec(), "c1", [event], _spec().metrics[0], evaluate,
        evaluator_version="ev-1", pass_threshold=0.5,
        evidence_event_ids=(), attempt=0,
    )
    assert score.status is CaseStatus.ERROR
    assert score.score is None
    assert "evaluator_runtime_failed" in score.message
