"""Tests for the TraceEvent envelope (src/llm_agent_eval/events.py).

Covers the §12B canonical envelope: the closed 14-type list, field shapes,
the typed `tool` column, JSONL round-trip, and the make_event constructor
(validated event_type, run_id/case_id required).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llm_agent_eval.events import (
    EVENT_TYPES,
    CostBlock,
    ErrorBlock,
    EventType,
    OtelContext,
    RedactionState,
    Source,
    TokenUsage,
    TraceEvent,
    make_event,
)


def mk(**fields):
    """A convenience wrapper around make_event with stable defaults."""
    return make_event(
        event_type=fields.pop("event_type", "llm_call"),
        run_id=fields.pop("run_id", "run_1"),
        case_id=fields.pop("case_id", "case_1"),
        workspace_id=fields.pop("workspace_id", "ws_1"),
        attempt_id=fields.pop("attempt_id", "att_1"),
        **fields,
    )


# ---------------------------------------------------------------------------
# make_event contract
# ---------------------------------------------------------------------------


def test_make_event_requires_run_id_and_case_id():
    with pytest.raises(TypeError):
        make_event(event_type="llm_call", run_id="r")  # no case_id
    with pytest.raises(TypeError):
        make_event(event_type="llm_call", case_id="c")  # no run_id
    with pytest.raises(TypeError):
        make_event(run_id="r", case_id="c")  # no event_type


def test_make_event_rejects_unknown_event_type_with_closed_list():
    with pytest.raises(ValueError) as excinfo:
        make_event(event_type="hallucination", run_id="r", case_id="c",
                   workspace_id="ws", attempt_id="a")
    message = str(excinfo.value)
    assert "hallucination" in message
    for t in EVENT_TYPES:
        assert t in message


def test_make_event_accepts_enum_and_string_event_types():
    assert mk(event_type=EventType.TOOL_CALL, tool="refund").type is EventType.TOOL_CALL
    assert mk(event_type="tool_call", tool="refund").type is EventType.TOOL_CALL


def test_make_event_accepts_every_closed_event_type():
    for t in EVENT_TYPES:
        kwargs = {"tool": "refund_order"} if t in ("tool_call", "tool_result") else {}
        ev = mk(event_type=t, **kwargs)
        assert ev.type.value == t


def test_make_event_defaults():
    ev = mk(event_type="llm_call", payload={"model": "x"})
    assert ev.repeat_index == 0
    assert ev.attempt == 0
    assert ev.sequence == 0
    assert ev.source is Source.PROXY
    assert ev.redaction_state.status == "clean"
    assert ev.redaction_state.rules == []
    assert ev.event_id  # auto-filled
    assert ev.timestamp is not None  # auto-filled
    assert ev.cost is None
    assert ev.duration_ms is None
    assert ev.error is None


def test_make_event_rejects_unknown_kwargs():
    # extra="forbid" surfaces as pydantic's ValidationError — still an
    # explicit rejection of the unknown field.
    with pytest.raises(ValidationError):
        mk(no_such_field=1)


# ---------------------------------------------------------------------------
# Model-level validation
# ---------------------------------------------------------------------------


def test_model_rejects_unknown_event_type():
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(
            mk(event_type="llm_call").model_dump() | {"type": "final_response"}
        )


def test_event_without_case_is_invalid():
    data = mk(event_type="llm_call").model_dump()
    del data["case_id"]
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(data)


def test_empty_identity_fields_are_rejected():
    for field in ("event_id", "run_id", "workspace_id", "case_id", "attempt_id"):
        with pytest.raises(ValidationError):
            mk(**{field: ""})


def test_source_values_are_closed():
    for value in ("proxy", "adapter", "runner"):
        assert mk(source=value).source.value == value
    with pytest.raises(ValidationError):
        mk(source="shim")


# ---------------------------------------------------------------------------
# The typed `tool` column (§12B.2)
# ---------------------------------------------------------------------------


def test_tool_required_on_tool_events():
    ev = mk(event_type="tool_call", tool="refund_order", payload={"args": {}})
    assert ev.tool == "refund_order"
    mk(event_type="tool_result", tool="refund_order", payload={"output": {}})
    with pytest.raises(ValidationError) as excinfo:
        mk(event_type="tool_call")
    assert "tool" in str(excinfo.value)
    with pytest.raises(ValidationError) as excinfo:
        mk(event_type="tool_result")
    assert "tool" in str(excinfo.value)


def test_tool_must_be_null_off_tool_events():
    with pytest.raises(ValidationError) as excinfo:
        mk(event_type="llm_call", tool="refund_order")
    assert "tool" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Envelope shapes
# ---------------------------------------------------------------------------


def test_payload_is_redacted_dict_or_none():
    assert mk(payload={"role": "user", "content": "hi"}).payload["role"] == "user"
    assert mk(payload=None).payload is None
    with pytest.raises(ValidationError):
        mk(payload="not a dict")


def test_redaction_state_status_is_closed():
    for status in ("clean", "redacted", "truncated"):
        ev = mk(redaction_state={"status": status, "rules": ["rule_1"]})
        assert ev.redaction_state.rules == ["rule_1"]
    with pytest.raises(ValidationError):
        mk(redaction_state={"status": "redacted_with_joy"})


def test_error_block_shape():
    ev = mk(error={"type": "provider_429", "message": "rate limited", "retryable": True})
    assert ev.error == ErrorBlock(type="provider_429", message="rate limited", retryable=True)
    with pytest.raises(ValidationError):
        mk(error={"type": "provider_429", "message": "rate limited"})  # no retryable


def test_cost_block_shape_and_free_events():
    ev = mk(cost=None)
    assert ev.cost is None  # absent for free events
    ev = mk(
        cost={
            "tokens": {"input": 100, "output": 50, "cache_read": 20, "cache_write": 0},
            "cost_usd": 0.0012,
            "currency": "USD",
            "price_version": "42",
        }
    )
    assert ev.cost.tokens.input == 100
    assert ev.cost.cost_usd == 0.0012
    assert ev.cost.currency == "USD"
    assert ev.cost.price_version == "42"
    with pytest.raises(ValidationError):
        mk(cost={"tokens": {"input": 1, "output": 1}, "cost_usd": 0.0, "currency": "EUR",
                 "price_version": "1"})
    with pytest.raises(ValidationError):
        mk(cost={"tokens": {"input": 1, "output": 1}, "cost_usd": 0.0, "currency": "USD"})


def test_otel_context_shape():
    ev = mk(otel={"trace_id": "t1", "span_id": "s1"})
    assert ev.otel == OtelContext(trace_id="t1", span_id="s1")
    assert mk(otel=None).otel is None


def test_timestamp_parses_iso8601():
    from datetime import datetime

    ev = mk(timestamp="2026-08-24T10:30:00Z")
    assert ev.timestamp == datetime.fromisoformat("2026-08-24T10:30:00Z")
    with pytest.raises(ValidationError):
        mk(timestamp="not a timestamp")


def test_extra_envelope_fields_are_rejected():
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(mk(event_type="llm_call").model_dump() | {"seq": 9})


# ---------------------------------------------------------------------------
# JSONL round-trip
# ---------------------------------------------------------------------------


def test_jsonl_round_trip_minimal():
    ev = mk(event_type="llm_call", payload={"model": "claude-opus-5"})
    line = ev.to_jsonl()
    assert json.loads(line)  # single valid JSON line
    assert TraceEvent.from_jsonl(line) == ev


def test_jsonl_round_trip_full_envelope():
    ev = mk(
        event_type="tool_result",
        tool="charge_payment",
        run_id="run_9",
        case_id="case_3",
        workspace_id="ws_7",
        attempt_id="att_2",
        repeat_index=2,
        attempt=1,
        sequence=14,
        timestamp="2026-08-24T10:30:00Z",
        duration_ms=12,
        parent_event_id="evt_parent",
        provider_request_id="req_abc",
        source="adapter",
        otel={"trace_id": "t1", "span_id": "s1"},
        cost={
            "tokens": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
            "cost_usd": 0.0001,
            "currency": "USD",
            "price_version": "42",
        },
        redaction_state={"status": "redacted", "rules": ["rule_api_key"]},
        payload={"tool": "charge_payment", "args": {"order_id": "123"}, "output": {"ok": True}},
        payload_ref="ws/ws_7/runs/run_9/payloads/att_2/evt_big.json",
        error=None,
    )
    line = ev.to_jsonl()
    restored = TraceEvent.from_jsonl(line)
    assert restored == ev
    assert restored.tool == "charge_payment"
    assert restored.source is Source.ADAPTER
    assert restored.redaction_state.status == "redacted"
    assert restored.cost.tokens.cache_read == 0
    assert restored.otel.span_id == "s1"
    assert restored.error is None


def test_jsonl_round_trip_iso8601_timestamps():
    ev = mk(timestamp="2026-08-24T10:30:00.123456Z")
    restored = TraceEvent.from_jsonl(ev.to_jsonl())
    assert restored.timestamp == ev.timestamp


def test_from_jsonl_rejects_invalid_line():
    with pytest.raises(ValidationError):
        TraceEvent.from_jsonl('{"not": "an event"}')
    with pytest.raises(Exception):
        TraceEvent.from_jsonl("this is not json")
