"""Unit tests for the trace capture client (src/llm_agent_eval/capture.py).

Covers the §12B envelope contract from the client's side: identity from the
runner's env contract, explicit ``redaction_state`` (the engine refuses events
without it — ``Engine._ingest_events``), the typed ``tool`` column, flush-per-
event crash safety, monotonic timestamp ordering from context entry, the no-op
sink without ``LLM_AGENT_EVAL_TRACE``, and JSONL round-trips via
``TraceEvent.from_jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_agent_eval.capture import TraceCapture
from llm_agent_eval.events import CostBlock, EventType, Source, TokenUsage, TraceEvent

IDENTITY = {
    "ENGINE_RUN_ID": "env-run",
    "ENGINE_CASE_ID": "env-case",
    "ENGINE_ATTEMPT_ID": "env-attempt",
    "ENGINE_WORKSPACE_ID": "env-ws",
}


@pytest.fixture()
def trace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the capture client at a temp trace path plus the runner's
    identity env contract."""
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("LLM_AGENT_EVAL_TRACE", str(trace))
    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)
    return trace


def read_events(trace: Path) -> list[TraceEvent]:
    return [
        TraceEvent.from_jsonl(line)
        for line in trace.read_text().splitlines()
        if line.strip()
    ]


def test_envelope_contract_and_payload_shapes(trace_env: Path) -> None:
    """The five helpers emit the canonical §12B envelope: source proxy,
    explicit redaction_state, typed tool column, and the §12B.3 payload
    shapes the trace-rule evaluators consume."""
    with TraceCapture() as cap:
        tc = cap.tool_call("search_articles", {"query": "refund"})
        tr = cap.tool_result("search_articles", {"matches": []})
        lc = cap.llm_call("mock-llm-1", [{"role": "user", "content": "hi"}])
        lr = cap.llm_response("mock-llm-1", "canned answer")
        dg = cap.delegation("refund_specialist", "resolve refund")

    events = read_events(trace_env)
    assert [e.event_id for e in events] == [e.event_id for e in (tc, tr, lc, lr, dg)]
    assert len(events) == 5
    for e in events:
        # Runner env contract fills identity on every event.
        assert e.run_id == "env-run"
        assert e.case_id == "env-case"
        assert e.attempt_id == "env-attempt"
        assert e.workspace_id == "env-ws"
        assert e.repeat_index == 0 and e.attempt == 0
        assert e.source is Source.PROXY
        # Explicit redaction_state — the engine rejects events without one.
        assert "redaction_state" in e.model_fields_set
        assert e.redaction_state.status == "clean"
        assert e.redaction_state.rules == []

    # Typed top-level tool column (§12B.2): set on tool events, null otherwise.
    assert [e.tool for e in events] == [
        "search_articles", "search_articles", None, None, None,
    ]
    assert [e.type for e in events] == [
        EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.LLM_CALL,
        EventType.LLM_RESPONSE, EventType.DELEGATION,
    ]

    # §12B.3 payload shapes.
    assert events[0].payload == {"args": {"query": "refund"}}
    assert events[1].payload == {"output": {"matches": []}, "status": "success"}
    assert events[2].payload == {
        "provider": "deepseek",
        "model": "mock-llm-1",
        "endpoint": "/chat/completions",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    assert events[3].payload == {
        "provider": "deepseek",
        "model": "mock-llm-1",
        "content": "canned answer",
    }
    assert events[4].payload == {"to_agent": "refund_specialist", "task": "resolve refund"}
    # Optional §12B.3 delegation fields.
    with TraceCapture() as cap:
        cap.delegation("sub", "task hint", decision="handoff", from_agent="triage")
    (dg2,) = read_events(trace_env)[-1:]
    assert dg2.payload == {
        "to_agent": "sub", "task": "task hint",
        "decision": "handoff", "from_agent": "triage",
    }


def test_jsonl_round_trip_is_lossless(trace_env: Path) -> None:
    with TraceCapture() as cap:
        cap.llm_response("m", "answer")
        cap.tool_call("t", {"x": 1})
        cap.tool_result("t", {"ok": True}, status="failure")
    events = read_events(trace_env)
    assert len(events) == 3
    for e in events:
        assert TraceEvent.from_jsonl(e.to_jsonl()) == e


def test_no_op_sink_without_trace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM_AGENT_EVAL_TRACE ⇒ every helper returns None and nothing is
    written — agents using the client also run standalone."""
    monkeypatch.delenv("LLM_AGENT_EVAL_TRACE", raising=False)
    cap = TraceCapture(run_id="r", case_id="c", workspace_id="w", attempt_id="a")
    with cap:
        assert cap.tool_call("t", {}) is None
        assert cap.tool_result("t", {}) is None
        assert cap.llm_call("m", []) is None
        assert cap.llm_response("m", "hint") is None
        assert cap.delegation("x", "y") is None
    assert not list(tmp_path.iterdir())


def test_no_op_sink_with_no_env_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare TraceCapture() in a clean environment is also a no-op sink
    (pure standalone agent run)."""
    for name in ("LLM_AGENT_EVAL_TRACE", *IDENTITY):
        monkeypatch.delenv(name, raising=False)
    with TraceCapture() as cap:
        assert cap.llm_response("m", "hint") is None
    assert not list(tmp_path.iterdir())


def test_trace_path_without_identity_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_AGENT_EVAL_TRACE", str(tmp_path / "trace.jsonl"))
    for name in IDENTITY:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="identity is incomplete"):
        TraceCapture()


def test_flush_per_event(trace_env: Path) -> None:
    """Each event is appended and flushed before the call returns — a killed
    agent's partial trace is already parseable evidence (§11A)."""
    with TraceCapture() as cap:
        cap.tool_call("t", {})
        # On disk while still inside the context.
        assert len(read_events(trace_env)) == 1
        cap.tool_result("t", {"ok": True})
        assert len(read_events(trace_env)) == 2
    assert len(read_events(trace_env)) == 2


def test_timestamps_and_sequence_are_monotonic(trace_env: Path) -> None:
    with TraceCapture() as cap:
        for i in range(5):
            cap.delegation(f"a{i}", "b")
    events = read_events(trace_env)
    stamps = [e.timestamp for e in events]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps), "timestamps must be strictly increasing"
    assert [e.sequence for e in events] == [0, 1, 2, 3, 4]


def test_identity_from_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """repeat_index/attempt come from the §11A manifest (the same source the
    runner writes), so repeats and retries carry their sample numbers."""
    trace = tmp_path / "trace.jsonl"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "manifest.json").write_text(
        json.dumps({"repeat_index": 2, "attempt": 1, "run_id": "ignored"})
    )
    monkeypatch.setenv("LLM_AGENT_EVAL_TRACE", str(trace))
    monkeypatch.setenv("LLM_AGENT_EVAL_INPUT", str(input_dir))
    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)
    with TraceCapture() as cap:
        cap.delegation("x", "y")
    (event,) = read_events(trace)
    assert event.repeat_index == 2
    assert event.attempt == 1


def test_explicit_arguments_override_env(trace_env: Path) -> None:
    with TraceCapture(run_id="explicit-run", case_id="explicit-case") as cap:
        e = cap.llm_call("m", [])
    assert e is not None
    assert e.run_id == "explicit-run"
    assert e.case_id == "explicit-case"
    assert e.attempt_id == "env-attempt"  # unset args still come from env


def test_cost_block_passes_through(trace_env: Path) -> None:
    cost = CostBlock(
        tokens=TokenUsage(input=10, output=20),
        cost_usd=0.00018,
        currency="USD",
        price_version="1999-12",  # deliberately wrong — the engine stamps its own
    )
    with TraceCapture() as cap:
        cap.llm_response("m", "answer", cost=cost)
    (event,) = read_events(trace_env)
    assert event.cost == cost
    assert event.cost.tokens.input == 10
    assert event.cost.tokens.output == 20
    assert event.cost.price_version == "1999-12"


def test_events_outside_context_raise(trace_env: Path) -> None:
    cap = TraceCapture()
    with pytest.raises(RuntimeError, match="inside the `with` block"):
        cap.tool_call("t", {})


def test_context_can_be_reentered(trace_env: Path) -> None:
    """A capture reused across attempts (one context per attempt) restarts
    timestamps and sequence and appends to the same trace file."""
    with TraceCapture() as cap:
        cap.delegation("a", "b")
    with TraceCapture() as cap:
        cap.delegation("c", "d")
    events = read_events(trace_env)
    assert len(events) == 2
    assert events[0].sequence == 0 and events[1].sequence == 0
    assert events[1].timestamp > events[0].timestamp
