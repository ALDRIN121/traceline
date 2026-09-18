from llm_agent_eval.retrieval import citation_correctness, recall_at_k
from llm_agent_eval.evaluators import CaseStatus, score_output
from llm_agent_eval.spec import EvaluationSpec


def test_retrieval_metrics_are_deterministic_and_fail_closed_on_missing_evidence():
    assert recall_at_k(["a", "b"], ["b", "c"], 2) == 0.5
    assert citation_correctness(["doc-1"], {"doc-1": True, "doc-2": False}) == 1.0
    assert citation_correctness(["missing"], {}) is None


def test_retrieval_evaluators_score_output_evidence_without_invoking_a_target():
    spec = EvaluationSpec.model_validate({
        "spec_version": "1", "name": "retrieval", "dataset_version": "d1",
        "cases": [{"case_id": "c1", "name": "one", "input": {}}],
        "metrics": [
            {
                "metric_id": "recall", "name": "recall", "type": "scalar",
                "target": {"type": "final_response", "selector": "$.retrieved", "on_missing": "fail"},
                "evaluator": {"type": "recall_at_k", "expected": ["a", "b"], "k": 2},
                "scoring": {"type": "numeric", "range": [0, 1]},
                "aggregation": {"method": "mean", "on_error": "fail"},
            },
            {
                "metric_id": "citations", "name": "citations", "type": "scalar",
                "target": {"type": "final_response", "selector": "$.citations", "on_missing": "fail"},
                "evaluator": {"type": "citation_correctness", "evidence": {"doc-1": True}},
                "scoring": {"type": "numeric", "range": [0, 1]},
                "aggregation": {"method": "mean", "on_error": "fail"},
            },
        ],
    })
    recall = score_output(spec, "c1", {"retrieved": ["b", "c"], "citations": ["doc-1"]}, spec.metrics[0])
    citations = score_output(spec, "c1", {"retrieved": ["b", "c"], "citations": ["doc-1"]}, spec.metrics[1])
    assert recall.score == 0.5
    assert recall.raw_value == 0.5
    assert citations.score == 1.0
    assert citations.status is CaseStatus.PASS
