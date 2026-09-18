"""The engine never substitutes a synthetic score for an unconfigured judge."""

import pytest

from llm_agent_eval.engine import Engine
from llm_agent_eval.evaluators import CaseStatus, evaluate_case
from llm_agent_eval.judge import JudgmentError, ScriptedJudge
from llm_agent_eval.spec import validate_spec
from llm_agent_eval.storage import Storage


def judge_spec():
    return validate_spec({
        "spec_version": "1", "name": "judge-default", "dataset_version": "v1",
        "cases": [{"case_id": "c1", "name": "first", "input": {"prompt": "fixture"}}],
        "metrics": [{
            "metric_id": "quality", "name": "quality", "type": "judge",
            "target": {"type": "input", "on_missing": "fail"},
            "evaluator": {"type": "llm_judge", "rubric_version_id": "rv1"},
            "scoring": {"type": "binary", "range": [0, 1], "condition": "raw_value >= 0.8"},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "judge_binding": {"provider": "fixture", "model": "fixture-model",
                              "schema_version": "1", "rubric_version": "1"},
            "provisional": True,
        }],
    })


def test_default_judge_refuses_to_fabricate_a_verdict(tmp_path):
    store = Storage(tmp_path / "db.sqlite")
    try:
        engine = Engine(store, work_root=tmp_path / "work")
        spec = judge_spec()
        metric = spec.metrics[0]
        judge = engine.judge_factory(metric.judge_binding)
        assert judge.binding == metric.judge_binding
        with pytest.raises(JudgmentError, match="must be configured"):
            judge.evaluate(metric, spec.cases[0], ())
        score = evaluate_case(spec, "c1", (), metric, judge=judge)
        assert score.status == CaseStatus.ERROR
        assert score.score is None
    finally:
        store.close()


def test_explicit_judge_factory_still_supplies_real_verdict_contract(tmp_path):
    store = Storage(tmp_path / "db.sqlite")
    try:
        engine = Engine(store, work_root=tmp_path / "work",
                        judge_factory=lambda binding: ScriptedJudge(binding, default_score=0.9))
        spec = judge_spec()
        metric = spec.metrics[0]
        score = evaluate_case(spec, "c1", (), metric,
                              judge=engine.judge_factory(metric.judge_binding))
        assert score.status == CaseStatus.PASS
        assert score.score == 1.0
        assert score.provisional
    finally:
        store.close()
