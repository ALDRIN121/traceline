from llm_agent_eval.retrieval import citation_correctness, recall_at_k


def test_retrieval_metrics_are_deterministic_and_fail_closed_on_missing_evidence():
    assert recall_at_k(["a", "b"], ["b", "c"], 2) == 0.5
    assert citation_correctness(["doc-1"], {"doc-1": True, "doc-2": False}) == 1.0
    assert citation_correctness(["missing"], {}) is None
