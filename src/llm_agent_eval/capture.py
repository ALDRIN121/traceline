"""In-process trace capture client — the MVP stand-in for the egress proxy.

Engine §12A/§12B, ledger R10: in the full design the recording egress proxy
captures every agent-under-test event; the MVP runs the agent as a plain
subprocess (runner.py), so a small in-process client stands in. The EVENT
contract is identical — this client emits the same §12B envelope via
``events.make_event`` and writes the same ``trace.jsonl`` the runner parses.

Contract, in the design's vocabulary:

- **Capture-time redaction** (§12A.6): the client never stores secrets. Every
  event carries an explicit ``redaction_state`` (the engine refuses to ingest
  an event without one — ``Engine._ingest_events``), and the payload helpers
  accept *hints*: already-truncated, already-redacted summaries (message
  content, tool args, outputs). The client never redacts and never contains
  full wire traffic.
- **Compromised-by-default in spirit** (R10): the client is a hint, never
  authority. The engine re-verifies every event on ingestion — identity
  claims (run/case/attempt), redaction_state presence, price-version stamping,
  and the 1,000-event cap. A broken or lying client cannot fabricate evidence.
- **Deterministic ordering** (§12B.4 fallback): ``timestamp`` is the client's
  wall clock at context entry plus monotonic elapsed milliseconds — strictly
  increasing regardless of wall-clock adjustment — and ``sequence`` is the
  per-attempt monotonic order. Together they give the ``(timestamp, sequence)``
  total order the trace-rule operators use for concurrent siblings.
- **Crash-safe writes**: every event is appended and flushed before the call
  returns; a killed agent's partial trace is still parseable evidence (the
  runner classifies a partial trace line exactly like a completed one — the
  engine scores only completed attempts, §13A.2, and never trusts a corrupt
  trace).

Identity comes from the runner's env contract (``LLM_AGENT_EVAL_TRACE``,
``ENGINE_RUN_ID`` / ``ENGINE_CASE_ID`` / ``ENGINE_ATTEMPT_ID`` /
``ENGINE_WORKSPACE_ID``) and from the §11A manifest
(``LLM_AGENT_EVAL_INPUT/manifest.json`` for ``repeat_index``/``attempt``);
explicit constructor arguments override the environment.

No ``LLM_AGENT_EVAL_TRACE`` in the environment ⇒ a no-op sink: every method
returns ``None`` and nothing is written, so agents that use the client also
run standalone (the sample fixture relies on this).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .events import (
    CostBlock,
    EventType,
    RedactionState,
    Source,
    TraceEvent,
    make_event,
)

__all__ = ["TraceCapture"]

_ENV_TRACE = "LLM_AGENT_EVAL_TRACE"
_ENV_INPUT = "LLM_AGENT_EVAL_INPUT"
_ENV_RUN = "ENGINE_RUN_ID"
_ENV_CASE = "ENGINE_CASE_ID"
_ENV_ATTEMPT = "ENGINE_ATTEMPT_ID"
_ENV_WORKSPACE = "ENGINE_WORKSPACE_ID"


def _manifest_repeat_attempt(input_dir: str | None) -> tuple[int, int]:
    """``repeat_index``/``attempt`` from the §11A manifest when the runner
    provided one; ``(0, 0)`` when it did not (standalone runs, smoke probes)."""
    if not input_dir:
        return 0, 0
    try:
        manifest = json.loads((Path(input_dir) / "manifest.json").read_text())
    except (OSError, ValueError):
        return 0, 0
    try:
        return int(manifest.get("repeat_index", 0)), int(manifest.get("attempt", 0))
    except (TypeError, ValueError):
        return 0, 0


def _last_trace_timestamp(path: Path) -> datetime | None:
    """Return the last parseable event timestamp from an existing trace."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(max(0, end - 8192))
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line:
            continue
        try:
            return TraceEvent.from_jsonl(line.decode("utf-8")).timestamp
        except (UnicodeDecodeError, ValueError):
            continue
    return None


class TraceCapture:
    """Context manager emitting §12B TraceEvents to the ``LLM_AGENT_EVAL_TRACE``
    JSONL file (append, flushed per event).

    Events are emitted only inside ``with TraceCapture():``. Explicit keyword
    arguments (``run_id``, ``case_id``, ``workspace_id``, ``attempt_id``,
    ``repeat_index``, ``attempt``, ``trace_path``, ``input_dir``) override the
    runner's env contract.

    With no ``LLM_AGENT_EVAL_TRACE`` the capture is a no-op sink — every
    method returns ``None`` and no file is created — so agent code that always
    traces also runs standalone.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        case_id: str | None = None,
        workspace_id: str | None = None,
        attempt_id: str | None = None,
        repeat_index: int | None = None,
        attempt: int | None = None,
        trace_path: str | Path | None = None,
        input_dir: str | Path | None = None,
    ):
        self._run_id = run_id if run_id is not None else os.environ.get(_ENV_RUN, "")
        self._case_id = case_id if case_id is not None else os.environ.get(_ENV_CASE, "")
        self._attempt_id = attempt_id if attempt_id is not None else os.environ.get(_ENV_ATTEMPT, "")
        self._workspace_id = (
            workspace_id if workspace_id is not None else os.environ.get(_ENV_WORKSPACE, "")
        )
        input_dir = input_dir if input_dir is not None else os.environ.get(_ENV_INPUT)
        manifest_repeat, manifest_attempt = _manifest_repeat_attempt(
            str(input_dir) if input_dir is not None else None
        )
        self._repeat_index = repeat_index if repeat_index is not None else manifest_repeat
        self._attempt = attempt if attempt is not None else manifest_attempt
        trace = trace_path if trace_path is not None else os.environ.get(_ENV_TRACE)
        self._trace_path = Path(trace) if trace else None
        if self._trace_path is not None and not (
            self._run_id and self._case_id and self._attempt_id and self._workspace_id
        ):
            raise ValueError(
                f"{_ENV_TRACE} is set but the attempt identity is incomplete — the "
                f"runner contract sets {_ENV_RUN} / {_ENV_CASE} / {_ENV_ATTEMPT} / "
                f"{_ENV_WORKSPACE}; pass them explicitly or run without "
                f"{_ENV_TRACE} (no-op sink)"
            )
        #: No trace path ⇒ no-op sink: agents using the client run standalone.
        self._enabled = self._trace_path is not None
        self._handle = None
        self._sequence = 0
        self._last_elapsed_ms = -1
        self._entry_dt: datetime | None = None
        self._entry_monotonic: float | None = None
        self._entered = False

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def __enter__(self) -> "TraceCapture":
        entry_dt = datetime.now(timezone.utc)
        previous = _last_trace_timestamp(self._trace_path) if self._enabled else None
        if previous is not None and entry_dt <= previous:
            entry_dt = previous + timedelta(microseconds=1)
        self._entry_dt = entry_dt
        self._entry_monotonic = time.monotonic()
        self._last_elapsed_ms = -1
        self._sequence = 0
        self._entered = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self._entered = False

    # ------------------------------------------------------------------
    # The event surface
    # ------------------------------------------------------------------

    def event(
        self,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        *,
        cost: CostBlock | None = None,
        tool: str | None = None,
        parent_event_id: str | None = None,
        provider_request_id: str | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent | None:
        """Build one §12B event and append it to the trace (flushed per
        event). Returns None when capture is a no-op sink (no
        ``LLM_AGENT_EVAL_TRACE``).

        ``payload`` is a capture-time hint — already redacted/truncated; this
        client never stores secrets. ``tool`` is the typed top-level column
        for ``tool_call``/``tool_result`` events (§12B.2).
        """
        if not self._enabled:
            return None
        if not self._entered:
            raise RuntimeError(
                "TraceCapture events must be emitted inside the `with` block"
            )
        event = make_event(
            event_type=event_type,
            run_id=self._run_id,
            case_id=self._case_id,
            workspace_id=self._workspace_id,
            attempt_id=self._attempt_id,
            timestamp=self._entry_dt + timedelta(milliseconds=self._elapsed_ms()),
            sequence=self._sequence,
            repeat_index=self._repeat_index,
            attempt=self._attempt,
            source=Source.PROXY,
            redaction_state=RedactionState(status="clean", rules=[]),
            payload=payload,
            cost=cost,
            tool=tool,
            parent_event_id=parent_event_id,
            provider_request_id=provider_request_id,
            duration_ms=duration_ms,
        )
        self._sequence += 1
        self._append(event)
        return event

    def tool_call(self, name: str, args: Any = None, **kw: Any) -> TraceEvent | None:
        """``tool_call`` with the typed ``tool`` column and §12B.3 payload
        ``{"args": ...}`` (args arrive already redacted/truncated)."""
        return self.event(EventType.TOOL_CALL, {"args": args}, tool=name, **kw)

    def tool_result(
        self, name: str, result: Any = None, *, status: str = "success", **kw: Any
    ) -> TraceEvent | None:
        """``tool_result`` — §12B.3 payload ``{"output": ..., "status": ...}``."""
        return self.event(
            EventType.TOOL_RESULT,
            {"output": result, "status": status},
            tool=name,
            **kw,
        )

    def llm_call(
        self,
        model: str,
        messages_hint: Any = None,
        *,
        provider: str | None = None,
        **kw: Any,
    ) -> TraceEvent | None:
        """``llm_call`` — §12B.3 payload ``{provider, model, endpoint,
        messages, stream}``; ``messages_hint`` is the already-truncated,
        already-redacted message summary."""
        return self.event(
            EventType.LLM_CALL,
            {
                "provider": provider or "deepseek",
                "model": model,
                "endpoint": "/chat/completions",
                "messages": messages_hint,
                "stream": False,
            },
            **kw,
        )

    def llm_response(
        self,
        model: str,
        content_hint: Any = None,
        *,
        cost: CostBlock | None = None,
        provider: str | None = None,
        **kw: Any,
    ) -> TraceEvent | None:
        """``llm_response`` — §12B.3 payload ``{provider, model, content}``;
        ``content_hint`` is the already-redacted content summary. ``cost`` is
        the proxy-computed cost block when the exchange was metered."""
        return self.event(
            EventType.LLM_RESPONSE,
            {"provider": provider or "deepseek", "model": model, "content": content_hint},
            cost=cost,
            **kw,
        )

    def delegation(
        self,
        target: str,
        task: Any = None,
        *,
        decision: Any = None,
        from_agent: str | None = None,
        **kw: Any,
    ) -> TraceEvent | None:
        """``delegation`` — §12B.3 payload ``{from_agent, to_agent, task,
        decision}``; ``task``/``decision`` are already-redacted summaries."""
        payload: dict[str, Any] = {"to_agent": target, "task": task}
        if decision is not None:
            payload["decision"] = decision
        if from_agent is not None:
            payload["from_agent"] = from_agent
        return self.event(EventType.DELEGATION, payload, **kw)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _elapsed_ms(self) -> int:
        """Monotonic elapsed milliseconds since context entry; strictly
        increasing even when the clock resolution would tie two events."""
        now = int((time.monotonic() - self._entry_monotonic) * 1000)
        if now <= self._last_elapsed_ms:
            now = self._last_elapsed_ms + 1
        self._last_elapsed_ms = now
        return now

    def _append(self, event: TraceEvent) -> None:
        if self._handle is None:
            self._handle = self._trace_path.open("a", encoding="utf-8")
        self._handle.write(event.to_jsonl() + "\n")
        # Flush per event (§11A crash-safety): a killed agent's partial trace
        # must be parseable evidence, never lost buffered content.
        self._handle.flush()
