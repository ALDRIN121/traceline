"""Capture-time-safe adapter evidence for remote R2 targets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..events import EventType, RedactionState, Source, TraceEvent, make_event
from ..redaction import redact


def adapter_event(
    execution_context: dict[str, Any],
    event_type: EventType | str,
    sequence: int,
    payload: dict[str, Any],
) -> TraceEvent | None:
    """Build an adapter event when the engine supplied attempt identity."""
    required = ("run_id", "workspace_id", "case_id", "attempt_id")
    if any(not isinstance(execution_context.get(key), str) or not execution_context[key] for key in required):
        return None
    safe = redact(payload)
    status = "truncated" if safe.truncated else ("redacted" if safe.detector_flags else "clean")
    content = safe.content if isinstance(safe.content, dict) else {"value": safe.content}
    return make_event(
        event_type=event_type,
        run_id=execution_context["run_id"],
        workspace_id=execution_context["workspace_id"],
        case_id=execution_context["case_id"],
        attempt_id=execution_context["attempt_id"],
        repeat_index=int(execution_context.get("repeat_index", 0)),
        attempt=int(execution_context.get("attempt", 0)),
        sequence=sequence,
        source=Source.ADAPTER,
        timestamp=datetime.now(timezone.utc),
        payload=content,
        redaction_state=RedactionState(status=status, rules=list(safe.detector_flags)),
    )
