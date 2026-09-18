from llm_agent_eval.targets.sessions import SessionManager


def test_stateful_sessions_are_unique_per_case_repeat_and_cleaned_up():
    calls = []
    manager = SessionManager(
        initialize=lambda session_id: calls.append(("init", session_id)),
        close=lambda session_id: calls.append(("close", session_id)),
    )
    first = manager.start("case-1", 0)
    second = manager.start("case-1", 1)
    assert first.session_id != second.session_id
    manager.turn(first.session_id, {"message": "hello"}, lambda *_: {"ok": True})
    manager.reset(first.session_id)
    manager.close_session(first.session_id)
    assert any(kind == "close" for kind, _ in calls)

