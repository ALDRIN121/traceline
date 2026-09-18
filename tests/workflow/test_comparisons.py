from __future__ import annotations

from llm_agent_eval.comparisons import ComparisonService, _bootstrap


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
