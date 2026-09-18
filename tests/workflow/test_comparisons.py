from __future__ import annotations

from types import SimpleNamespace

from llm_agent_eval.comparisons import ComparisonService, _bootstrap
from llm_agent_eval.spec import Metric, validate_spec


def test_bootstrap_is_seeded_and_bounded():
    assert _bootstrap([0.1, 0.2, 0.3], 100, 7) == _bootstrap([0.1, 0.2, 0.3], 100, 7)


def test_comparison_returns_incomparable_for_missing_run(tmp_path):
    from llm_agent_eval.storage import Storage
    storage = Storage(tmp_path / "comparison.db")
    storage.create_schema()
    try:
        try:
            ComparisonService(storage).compare("ws", "missing", "missing2", "m")
        except Exception as error:
            assert getattr(error, "code", None) == "not_found"
    finally:
        storage.close()


def _metric():
    return Metric.model_validate({
        "metric_id": "quality", "name": "quality", "type": "scalar",
        "target": {"type": "final_response", "selector": "$.answer", "on_missing": "fail"},
        "evaluator": {"type": "exact_match", "expected": "ok"},
        "scoring": {"type": "binary", "range": [0, 1]},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "gate": {"min": 0.5}, "provisional": False,
    })


def test_comparison_discloses_confounders_and_withholds_ci_for_low_repeats():
    metric = _metric()
    spec = SimpleNamespace(metrics=(metric,), cases=(
        SimpleNamespace(case_id="a", expected={}),
        SimpleNamespace(case_id="b", expected={}),
    ))
    run = lambda run_id, source: SimpleNamespace(
        run_id=run_id, status="complete", repeats=1, spec=spec,
        world_config={"version_refs": {
            "dataset_version_id": "d1", "source_version_id": source,
            "world_version_id": "world1",
        }},
    )
    cases = {
        "base": [SimpleNamespace(case_key="ka", case_id="a"), SimpleNamespace(case_key="kb", case_id="b")],
        "candidate": [SimpleNamespace(case_key="ka", case_id="a"), SimpleNamespace(case_key="kb", case_id="b")],
    }
    scores = {
        "base": [SimpleNamespace(case_id="a", score=1.0, is_authoritative=True, status="PASS"),
                 SimpleNamespace(case_id="b", score=0.0, is_authoritative=True, status="FAIL")],
        "candidate": [SimpleNamespace(case_id="a", score=1.0, is_authoritative=True, status="PASS"),
                     SimpleNamespace(case_id="b", score=1.0, is_authoritative=True, status="PASS")],
    }
    class FakeStorage:
        def get_run(self, run_id, workspace_id):
            return run_id and run(run_id, "source-base" if run_id == "base" else "source-candidate")
        def list_cases(self, run_id, workspace_id):
            return cases[run_id]
        def get_case_scores(self, **kwargs):
            return scores[kwargs["run_id"]]

    result = ComparisonService(FakeStorage()).compare("ws", "base", "candidate", "quality")
    assert result["state"] == "comparable"
    assert result["confidence_interval"] is None
    assert result["no_ci"] is True
    assert result["confounders"][0]["kind"] == "source_version_id"
    assert result["cohort"]["scored_common_cases"] == 2


def test_comparison_rejects_changed_reference_values():
    metric = _metric()
    left_case = SimpleNamespace(case_id="a", expected={"answer": SimpleNamespace(model_dump=lambda **_: {"value": "ok"})})
    right_case = SimpleNamespace(case_id="a", expected={"answer": SimpleNamespace(model_dump=lambda **_: {"value": "different"})})
    spec_left = SimpleNamespace(metrics=(metric,), cases=(left_case,))
    spec_right = SimpleNamespace(metrics=(metric,), cases=(right_case,))
    class FakeStorage:
        def get_run(self, run_id, workspace_id):
            return SimpleNamespace(run_id=run_id, status="complete", repeats=3,
                                   spec=spec_left if run_id == "base" else spec_right,
                                   world_config={})
        def list_cases(self, run_id, workspace_id):
            return [SimpleNamespace(case_key="same-input", case_id="a")]
        def get_case_scores(self, **kwargs):
            return []
    result = ComparisonService(FakeStorage()).compare("ws", "base", "candidate", "quality")
    assert result == {"state": "incomparable", "reason": "reference_version_changed", "common_cases": 1}
