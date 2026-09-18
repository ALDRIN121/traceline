"""Agent invocation — one fresh subprocess per case attempt (engine §11A).

R10 divergence, stated: the MVP runs the agent under test as a plain
subprocess instead of the egress-proxy + Podman runtime. Every invariant that
does not need the sandbox holds here:

- **Fresh process per case** (invariant): each attempt is its own subprocess;
  agent state can never leak between cases.
- **The §11A file protocol** (per attempt, under ``work_dir``):
  ``input/case.json`` (the case's input payload), ``input/manifest.json``
  (the attempt's identity — the manifest **never carries an ``expected``
  section**, §11A.3: the agent must not be able to game its own scoring),
  ``output/result.json`` (written atomically by the agent:
  ``.result.json.tmp`` -> fsync -> rename), and ``trace.jsonl`` (the agent's
  trace, JSONL).
- **Environment**: ``LLM_AGENT_EVAL_INPUT``, ``LLM_AGENT_EVAL_OUTPUT``,
  ``LLM_AGENT_EVAL_TRACE``, ``ENGINE_RUN_ID``, ``ENGINE_CASE_ID``,
  ``ENGINE_ATTEMPT_ID``, ``ENGINE_WORKSPACE_ID``. The parent environment is
  passed through (the MVP has no sandbox to constrain it — that is R10's
  egress story).
- **Exit code contract** (§11A.5): exit 0 = completed. Any nonzero exit is an
  agent failure — ``invocation_failed`` — and is *never scored as an
  assertion*. Runner-applied kills are lifecycle statuses
  (``timed_out``), never agent exit codes (§13A.2).
- **Timeout** (§11A): ``SIGTERM`` to the whole process group, 10 s grace,
  then ``SIGKILL``. A timeout-as-kill is never success.
- **stdout/stderr** are capped at 1 MiB each; overflow is marked, never
  stored unbounded.
- **The trace is the source of truth**: a clean run with no trace file (or an
  empty one) is the ``no_trace`` condition — a real state, never a fabricated
  trace. A malformed trace line is ``invocation_failed`` (a corrupt trace
  cannot be trusted as evidence).

Classification order (the single decision table):

1. spawn ``OSError`` -> ``invocation_failed``
2. timeout -> ``timed_out``
3. ``exit_code != 0`` -> ``invocation_failed``
4. ``result.json`` missing or unparseable -> ``invocation_failed``
5. ``result.json`` ``status == "error"`` with ``error.type ==
   "provider_unreachable"`` -> ``provider_unreachable`` (the agent ran, but
   its model provider was unreachable)
6. ``result.json`` ``status == "error"`` otherwise -> ``invocation_failed``
7. trace file missing or empty -> ``no_trace``
8. trace unparseable (any line) -> ``invocation_failed``
9. else -> ``completed``

Events are parsed only for the two success-shaped statuses (``completed`` /
``no_trace``); a killed or failed attempt's partial trace is not ingested —
the engine scores only completed attempts (§13A.2) and never treats a partial
trace as evidence of success.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .events import TraceEvent
from .spec import EvaluationSpec, TestCase

__all__ = [
    "InvocationResult",
    "invoke_agent",
    "COMPLETED",
    "TIMED_OUT",
    "INVOCATION_FAILED",
    "PROVIDER_UNREACHABLE",
    "NO_TRACE",
    "CANCELLED",
]

COMPLETED = "completed"
TIMED_OUT = "timed_out"
INVOCATION_FAILED = "invocation_failed"
PROVIDER_UNREACHABLE = "provider_unreachable"
NO_TRACE = "no_trace"
CANCELLED = "cancelled"

#: Grace period between SIGTERM and SIGKILL (§11A).
KILL_GRACE_SECONDS = 10.0
#: Per-stream output cap (§11A: 1 MiB each).
OUTPUT_CAP_BYTES = 1_048_576
#: A trace file larger than this is not evidence, it is a broken agent.
MAX_TRACE_FILE_BYTES = 64 * 1_048_576


@dataclass(frozen=True)
class InvocationResult:
    """The outcome of one agent invocation.

    ``status`` is one of the classification constants above. ``trace_events``
    is populated only for ``completed``/``no_trace``; ``result_payload`` is
    the parsed ``result.json`` (whole document, §9A.1's final-response surface
    included for stage-2 consumers). ``error`` carries the human-readable
    failure detail whenever the status is not success-shaped.
    """

    status: str
    exit_code: int | None
    trace_events: tuple[TraceEvent, ...] = ()
    result_payload: dict[str, Any] | None = None
    error: str | None = None
    trace_truncated: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""


def invoke_agent(
    *,
    agent_command: Sequence[str],
    run_id: str,
    workspace_id: str,
    case: TestCase,
    attempt_id: str,
    repeat_index: int,
    attempt: int,
    spec: EvaluationSpec,
    timeout_seconds: float,
    work_dir: Path,
    cwd: str | Path | None = None,
    env_extra: Mapping[str, str] | None = None,
) -> InvocationResult:
    """Run one case attempt. ``work_dir`` is created fresh per attempt (input/,
    output/, trace.jsonl live under it); the parent process must pass a
    per-attempt directory so artifacts persist as evidence."""
    work_dir = Path(work_dir)
    input_dir = work_dir / "input"
    output_dir = work_dir / "output"
    trace_path = work_dir / "trace.jsonl"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_input_files(input_dir, case, spec, run_id, attempt_id, repeat_index, attempt)

    env = dict(os.environ)
    env.update(
        {
            "LLM_AGENT_EVAL_TRACE": str(trace_path),
            "LLM_AGENT_EVAL_INPUT": str(input_dir),
            "LLM_AGENT_EVAL_OUTPUT": str(output_dir),
            "ENGINE_RUN_ID": run_id,
            "ENGINE_CASE_ID": case.case_id,
            "ENGINE_ATTEMPT_ID": attempt_id,
            "ENGINE_WORKSPACE_ID": workspace_id,
        }
    )
    if env_extra:
        env.update(env_extra)

    command = list(agent_command)
    if not command:
        return InvocationResult(
            INVOCATION_FAILED, None, error="agent command is empty — no entrypoint resolved"
        )

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group: SIGTERM reaches children too
        )
    except OSError as exc:
        return InvocationResult(
            INVOCATION_FAILED, None, error=f"could not spawn agent: {exc}"
        )

    stdout_data: list[bytes] = []
    stderr_data: list[bytes] = []
    stdout_overflow = [False]
    stderr_overflow = [False]

    def _capture(pipe: Any, sink: list[bytes], overflow: list[bool]) -> None:
        total = 0
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            if total < OUTPUT_CAP_BYTES:
                room = OUTPUT_CAP_BYTES - total
                sink.append(chunk[:room])
                total += len(chunk[:room])
                if len(chunk) > room:
                    overflow[0] = True
            else:
                overflow[0] = True

    stdout_thread = threading.Thread(target=_capture, args=(proc.stdout, stdout_data, stdout_overflow), daemon=True)
    stderr_thread = threading.Thread(target=_capture, args=(proc.stderr, stderr_data, stderr_overflow), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # Runner-applied kill: SIGTERM -> grace -> SIGKILL (§11A). The kill is
        # a lifecycle status, never an agent exit code.
        exit_code = _terminate_process(proc)
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        return InvocationResult(
            TIMED_OUT,
            exit_code,
            error=f"timed out after {timeout_seconds:g}s (SIGTERM, {KILL_GRACE_SECONDS:g}s grace, then SIGKILL)",
            stdout_tail=_tail(b"".join(stdout_data), stdout_overflow[0]),
            stderr_tail=_tail(b"".join(stderr_data), stderr_overflow[0]),
        )
    stdout_thread.join(timeout=2.0)
    stderr_thread.join(timeout=2.0)
    stdout_text = _tail(b"".join(stdout_data), stdout_overflow[0])
    stderr_text = _tail(b"".join(stderr_data), stderr_overflow[0])

    # Decision table, in order (see module docstring).
    if exit_code != 0:
        return InvocationResult(
            INVOCATION_FAILED, exit_code,
            error=f"agent exited {exit_code} (§11A.5: nonzero exit is an agent failure, never scored as an assertion)",
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )

    result_payload, result_error = _read_result_json(output_dir / "result.json")
    if result_payload is None:
        return InvocationResult(
            INVOCATION_FAILED, exit_code, error=result_error,
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )
    status = result_payload.get("status")
    if status == "error":
        error_block = result_payload.get("error") or {}
        if error_block.get("type") == "provider_unreachable":
            return InvocationResult(
                PROVIDER_UNREACHABLE, exit_code,
                result_payload=result_payload,
                error=error_block.get("message") or "provider unreachable",
                stdout_tail=stdout_text, stderr_tail=stderr_text,
            )
        return InvocationResult(
            INVOCATION_FAILED, exit_code, result_payload=result_payload,
            error=error_block.get("message") or f"agent reported status {status!r}",
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )
    if status != "completed":
        return InvocationResult(
            INVOCATION_FAILED, exit_code, result_payload=result_payload,
            error=f"result.json status must be 'completed' or 'error', got {status!r}",
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )

    events, trace_error = _read_trace(trace_path)
    if trace_error is not None:
        return InvocationResult(
            INVOCATION_FAILED, exit_code, error=trace_error,
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )
    if not events:
        return InvocationResult(
            NO_TRACE, exit_code, result_payload=result_payload,
            error="the agent completed but wrote no trace events",
            stdout_tail=stdout_text, stderr_tail=stderr_text,
        )
    return InvocationResult(
        COMPLETED, exit_code, trace_events=tuple(events),
        result_payload=result_payload, stdout_tail=stdout_text, stderr_tail=stderr_text,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_input_files(
    input_dir: Path,
    case: TestCase,
    spec: EvaluationSpec,
    run_id: str,
    attempt_id: str,
    repeat_index: int,
    attempt: int,
) -> None:
    # case.json — the case's input payload. Never the expected values.
    (input_dir / "case.json").write_text(
        json.dumps(
            {
                "case_id": case.case_id,
                "name": case.name,
                "description": case.description,
                "input": case.input,
                "metadata": case.metadata,
            },
            sort_keys=True,
        )
    )
    # manifest.json — the attempt's identity. Deliberately NO `expected`
    # section (§11A.3): the agent under test must not be able to read its own
    # answer key.
    (input_dir / "manifest.json").write_text(
        json.dumps(
            {
                "engine_version": __version__,
                "spec_name": spec.name,
                "spec_version": spec.spec_version,
                "dataset_version": spec.dataset_version,
                "run_id": run_id,
                "case_id": case.case_id,
                "attempt_id": attempt_id,
                "repeat_index": repeat_index,
                "attempt": attempt,
            },
            sort_keys=True,
        )
    )


def _terminate_process(proc: subprocess.Popen) -> int | None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        return proc.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        return proc.wait(timeout=KILL_GRACE_SECONDS)


def _read_result_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (payload, None) on success, (None, message) on failure."""
    if not path.exists():
        return None, f"result.json not found at {path} — the agent must write it atomically (§11A)"
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, f"result.json is unreadable: {exc}"
    if not isinstance(payload, dict):
        return None, f"result.json must be a JSON object, got {type(payload).__name__}"
    return payload, None


def _read_trace(path: Path) -> tuple[list[TraceEvent], str | None]:
    """Returns (events, None) on success, ([], message) on failure."""
    if not path.exists():
        return [], None  # no_trace: a clean run with no trace file
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], f"trace file unreadable: {exc}"
    if size == 0:
        return [], None  # no_trace: empty trace
    if size > MAX_TRACE_FILE_BYTES:
        return [], (
            f"trace file is {size} bytes — above the {MAX_TRACE_FILE_BYTES}-byte cap; "
            "a trace this large is not evidence, it is a broken agent"
        )
    events: list[TraceEvent] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(TraceEvent.from_jsonl(stripped))
                except ValueError as exc:
                    return [], f"trace line {lineno} is not a valid TraceEvent: {exc}"
    except OSError as exc:
        return [], f"trace file unreadable: {exc}"
    return events, None


def _tail(data: bytes, overflow: bool) -> str:
    text = data.decode("utf-8", errors="replace")
    tail = text[-4096:] if len(text) > 4096 else text
    if overflow:
        tail = f"{tail}\n...[output exceeded 1 MiB, truncated]"
    return tail
