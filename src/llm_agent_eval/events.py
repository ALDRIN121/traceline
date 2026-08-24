"""TraceEvent envelope for agent-under-test traces (engine §12B).

The canonical envelope, taken verbatim from §12B.2:

    event_id, run_id, workspace_id, case_id, attempt_id, repeat_index,
    attempt, sequence, type, source, timestamp, duration_ms,
    parent_event_id, provider_request_id, otel{trace_id, span_id},
    cost{tokens{input, output, cache_read, cache_write}, cost_usd,
    currency, price_version}, redaction_state{status, rules[]}, payload,
    payload_ref, error{type, message, retryable}

The event-type list is the closed 14-type contract (§12B.1): ``llm_call``,
``llm_response``, ``tool_call``, ``tool_result``, ``error``, ``retrieval``,
``delegation``, ``guardrail_check``, ``wait_for_input``, ``user_response``,
``stream_start``, ``first_token``, ``retry``, ``budget_exceeded``. No type may
be added or renamed without a trace-schema version bump.

Two surfaces are never conflated here: these events are **agent-under-test
traces** — never harness sessions (harness conversations live in
``harness_messages`` and are the harness writer's concern).

Redaction happens at capture (engine §12A.6): payloads arrive **already
redacted**, and this module never redacts and never contains secrets — it only
carries ``redaction_state`` so downstream consumers can trust the stream. A
cassette never contains a pre-redaction payload, and no event is ever
un-redacted by this layer.

Concurrency semantics of ``parent_event_id`` (engine §12B.4) are *used* by
trace-rule operators, not enforced here: ``parent_event_id`` is an opaque
causal pointer; sibling events share a parent and are ordered by the
``(timestamp, sequence)`` fallback.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EventType",
    "EVENT_TYPES",
    "Source",
    "TokenUsage",
    "CostBlock",
    "RedactionState",
    "OtelContext",
    "ErrorBlock",
    "TraceEvent",
    "make_event",
]


class EventType(str, Enum):
    """The closed 14-type event list (engine §12B.1, canonical).

    Semantics' §9A ``match.event`` keys and §18's DDL are built on exactly
    this set; no type may be added or renamed without a trace-schema version
    bump.
    """

    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    RETRIEVAL = "retrieval"
    DELEGATION = "delegation"
    GUARDRAIL_CHECK = "guardrail_check"
    WAIT_FOR_INPUT = "wait_for_input"
    USER_RESPONSE = "user_response"
    STREAM_START = "stream_start"
    FIRST_TOKEN = "first_token"
    RETRY = "retry"
    BUDGET_EXCEEDED = "budget_exceeded"


#: The closed event-type list as plain strings, for validation messages and
#: trace-rule ``match.event`` checks.
EVENT_TYPES: tuple[str, ...] = tuple(t.value for t in EventType)


class Source(str, Enum):
    """Who authored the event (engine §12B.2).

    ``proxy`` — wire capture; ``adapter`` — shim enrichment via /_enrich,
    stored as adapter-authored; ``runner`` — worker-written case-level
    ``error`` events for pre-trace failures.
    """

    PROXY = "proxy"
    ADAPTER = "adapter"
    RUNNER = "runner"


class TokenUsage(BaseModel):
    """Token usage inside the cost block (engine §12B.2)."""

    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


class CostBlock(BaseModel):
    """Proxy-computed cost; absent (None) on the event for free events."""

    model_config = ConfigDict(extra="forbid")

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float
    currency: Literal["USD"] = "USD"
    #: From the proxy's versioned price table (engine §12A.5); historical
    #: totals survive provider price drift only with this version recorded.
    price_version: str


class RedactionState(BaseModel):
    """Record of capture-time redaction (engine §12B.2/§12A.6).

    ``rules`` are rule ids applied — never the redacted content. The default
    ``clean``/``[]`` is truthful for an event that held no secrets; events are
    redacted at capture, so anything other than ``clean`` means rules fired.
    This module never redacts; it only records the state.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["clean", "redacted", "truncated"] = "clean"
    rules: list[str] = Field(default_factory=list)


class OtelContext(BaseModel):
    """OpenTelemetry correlation ids — enrichment, never authority."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = None
    span_id: str | None = None


class ErrorBlock(BaseModel):
    """The ``error`` block; completion is expressed by paired events, not a
    status column (engine §12B.4): ``llm_call`` → ``llm_response`` (or
    ``error`` + optional ``retry``), ``tool_call`` → ``tool_result``."""

    model_config = ConfigDict(extra="forbid")

    type: str
    message: str
    retryable: bool


class TraceEvent(BaseModel):
    """One event in an agent-under-test trace (engine §12B, canonical).

    ``timestamp`` is the proxy wall clock (ISO 8601, authoritative);
    ``sequence`` is the per-attempt monotonic order assigned by the proxy and
    is the deterministic fallback order for concurrent siblings (§12B.4).
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)  # RANGE-partition key; no FK to runs (decision 4F)
    workspace_id: str = Field(min_length=1)  # RLS key (decision 3)
    case_id: str = Field(min_length=1)  # an event without a case is invalid
    attempt_id: str = Field(min_length=1)  # one row in run_case_attempts
    repeat_index: int = 0  # 0-based repeat sample (§11C)
    attempt: int = 0  # 0-based retry attempt; first attempt = 0
    sequence: int = 0  # per-attempt monotonic order, assigned by the proxy
    type: EventType
    source: Source = Source.PROXY
    timestamp: datetime  # proxy wall clock — authoritative
    duration_ms: int | None = None
    parent_event_id: str | None = None
    provider_request_id: str | None = None
    #: Typed top-level column for ``tool_call``/``tool_result`` (null
    #: otherwise) — engine §12B.2.
    tool: str | None = None
    otel: OtelContext | None = None
    #: Proxy-computed from raw usage; absent (None) for free events.
    cost: CostBlock | None = None
    redaction_state: RedactionState = Field(default_factory=RedactionState)
    #: Redacted jsonb, ≤ 32 KB in-row; already redacted at capture — events
    #: never contain secrets, and this module never redacts.
    payload: dict[str, Any] | None = None
    payload_ref: str | None = None  # object-storage key when payload overflows
    error: ErrorBlock | None = None

    @model_validator(mode="after")
    def _tool_typed_column(self) -> "TraceEvent":
        """`tool` is the typed column for tool_call/tool_result, null
        otherwise (engine §12B.2)."""
        if self.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
            if self.tool is None:
                raise ValueError(
                    f"tool is required on {self.type.value} events "
                    "(typed top-level column, §12B.2)"
                )
        elif self.tool is not None:
            raise ValueError(
                f"tool must be null on {self.type.value} events; the typed "
                "tool column is for tool_call/tool_result only (§12B.2)"
            )
        return self

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line (round-trips via from_jsonl)."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> "TraceEvent":
        """Parse one JSONL line back into a TraceEvent."""
        return cls.model_validate_json(line)


def make_event(
    *,
    event_type: EventType | str,
    run_id: str,
    case_id: str,
    workspace_id: str,
    attempt_id: str,
    event_id: str | None = None,
    timestamp: datetime | None = None,
    **fields: Any,
) -> TraceEvent:
    """Construct a TraceEvent with validated ``event_type``.

    Requires ``run_id`` and ``case_id`` — an event without a case is invalid.
    ``event_type`` is validated against the closed 14-type list up front with
    a clear error. ``event_id`` and ``timestamp`` are filled in when omitted
    (uuid4 hex and the current UTC wall clock); everything else defaults per
    the envelope (repeat 0, attempt 0, sequence 0, source ``proxy``,
    redaction_state ``clean``).

    Raises:
        ValueError: if ``event_type`` is not one of the closed event types.
    """
    try:
        resolved_type = EventType(event_type)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"invalid event_type {event_type!r}; must be one of {EVENT_TYPES}"
        ) from exc
    return TraceEvent(
        event_id=event_id if event_id is not None else uuid.uuid4().hex,
        run_id=run_id,
        workspace_id=workspace_id,
        case_id=case_id,
        attempt_id=attempt_id,
        timestamp=timestamp if timestamp is not None else datetime.now(timezone.utc),
        type=resolved_type,
        **fields,
    )
