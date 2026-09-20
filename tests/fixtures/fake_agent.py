#!/usr/bin/env python3
"""The fake agent under test — a deliberately small, stdlib-only agent.

It exercises the §11A invocation protocol from the *agent's* side:

- reads ``case.json`` / ``manifest.json`` from ``LLM_AGENT_EVAL_INPUT``
- writes ``trace.jsonl`` to ``LLM_AGENT_EVAL_TRACE`` (a 3-event trace:
  ``tool_call`` -> ``tool_result`` -> ``llm_response``; the tool_result's
  ``payload.output.amount`` is what the spec's scalar metric asserts on)
- writes ``result.json`` atomically (``.result.json.tmp`` -> fsync -> rename)
  into ``LLM_AGENT_EVAL_OUTPUT``
- echoes the manifest's identity fields into ``result.json`` so tests can
  verify the protocol actually delivered them

Every deviation a test needs is a mode, selected by ``FAKE_AGENT_MODE``:

``ok`` (default)      happy path
``no_trace``          completes but writes no trace file
``empty_trace``       writes an empty trace file
``fail_exit``         exits 3 (§11A.5: nonzero exit = agent failure)
``missing_result``    exits 0 without writing result.json
``bad_result``        writes invalid JSON as result.json
``error_result``      result.json status "error" (generic)
``provider_unreachable``  result.json status "error", error.type
                        "provider_unreachable"
``sleep``             sleeps FAKE_AGENT_SLEEP_SECONDS (default 30) — for the
                        timeout-kill test
``leak``              appends to a module-global list before running — the
                        fresh-process-per-case test asserts each process
                        observes an empty list
``no_redaction_state``    trace events omit redaction_state (capture-contract
                        violation, §12A.6 — the engine must refuse)
``wrong_ids``         trace events claim a different run/case/attempt than
                        the invocation (contamination — the engine must refuse)
``garbage_trace``     trace.jsonl contains non-JSON lines
``huge_trace``        1,050 events — the engine's per-attempt cap is 1,000
                        (§18 Q8); overflow must be marked trace_truncated

Amount knobs (the value asserted by the scalar metric, expected 49.99):

``FAKE_AGENT_OUTPUT_AMOUNT``            written by every attempt (default 49.99)
``FAKE_AGENT_OUTPUT_AMOUNT_ATTEMPT_0``  overrides attempt == 0 — first-attempt
                                        authority tests
``FAKE_AGENT_OUTPUT_AMOUNT_REPEAT_0``   overrides repeat_index == 0 —
                                        flakiness tests

Trace events carry ``cost`` with ``price_version`` "1999-01" (deliberately
wrong) so the engine's ingestion-time stamping to the configured
``price_version`` is observable.
"""

import json
import os
import sys
import time
from pathlib import Path

LEAK = []  # module-global on purpose: the leak test asserts per-process isolation

MODE = os.environ.get("FAKE_AGENT_MODE", "ok")
INPUT_DIR = Path(os.environ["LLM_AGENT_EVAL_INPUT"])
OUTPUT_DIR = Path(os.environ["LLM_AGENT_EVAL_OUTPUT"])
TRACE_PATH = Path(os.environ["LLM_AGENT_EVAL_TRACE"])
RUN_ID = os.environ.get("ENGINE_RUN_ID", "")
CASE_ID = os.environ.get("ENGINE_CASE_ID", "")
ATTEMPT_ID = os.environ.get("ENGINE_ATTEMPT_ID", "")
WORKSPACE_ID = os.environ.get("ENGINE_WORKSPACE_ID", "")
MAX_EVENTS = 1000

TIMESTAMP = "2026-01-01T00:00:00.000Z"


def _amount() -> float:
    attempt = int(_manifest().get("attempt", 0))
    repeat = int(_manifest().get("repeat_index", 0))
    if attempt == 0 and os.environ.get("FAKE_AGENT_OUTPUT_AMOUNT_ATTEMPT_0") is not None:
        return float(os.environ["FAKE_AGENT_OUTPUT_AMOUNT_ATTEMPT_0"])
    if repeat == 0 and os.environ.get("FAKE_AGENT_OUTPUT_AMOUNT_REPEAT_0") is not None:
        return float(os.environ["FAKE_AGENT_OUTPUT_AMOUNT_REPEAT_0"])
    return float(os.environ.get("FAKE_AGENT_OUTPUT_AMOUNT", "49.99"))


_manifest_cache: dict | None = None


def _manifest() -> dict:
    global _manifest_cache
    if _manifest_cache is None:
        _manifest_cache = json.loads((INPUT_DIR / "manifest.json").read_text())
    return _manifest_cache


def _event(event_id, event_type, sequence, *, tool=None, payload=None, cost=True, redaction=True):
    e = {
        # Namespaced per attempt: trace_events' PK is (run_id, event_id) — a
        # real proxy assigns UUIDs, and colliding ids across attempts would
        # silently drop evidence.
        "event_id": f"{ATTEMPT_ID}-{event_id}",
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "case_id": CASE_ID,
        "attempt_id": ATTEMPT_ID,
        "repeat_index": int(_manifest().get("repeat_index", 0)),
        "attempt": int(_manifest().get("attempt", 0)),
        "sequence": sequence,
        "type": event_type,
        "source": "proxy",
        "timestamp": TIMESTAMP,
        "duration_ms": 5,
        "tool": tool,
    }
    if cost:
        e["cost"] = {
            "tokens": {"input": 10, "output": 20, "cache_read": 0, "cache_write": 0},
            "cost_usd": 0.0012,
            "currency": "USD",
            "price_version": "1999-01",
        }
    if redaction:
        e["redaction_state"] = {"status": "clean", "rules": []}
    if payload is not None:
        e["payload"] = payload
    return e


def _trace_lines(amount: float) -> list[str]:
    if MODE == "no_redaction_state":
        lines = [
            json.dumps(_event("ev1", "tool_call", 0, tool="refund_order", redaction=False)),
            json.dumps(_event("ev2", "tool_result", 1, tool="refund_order",
                              payload={"output": {"amount": amount}}, redaction=False)),
            json.dumps(_event("ev3", "llm_response", 2, redaction=False)),
        ]
    elif MODE == "wrong_ids":
        base = _event("ev1", "tool_call", 0, tool="refund_order")
        base["run_id"] = "other-run"
        lines = [json.dumps(base)]
    elif MODE == "huge_trace":
        lines = [
            json.dumps(_event(f"ev{i}", "tool_call", i, tool="refund_order"))
            for i in range(MAX_EVENTS + 50)
        ]
    else:
        lines = [
            json.dumps(_event("ev1", "tool_call", 0, tool="refund_order")),
            json.dumps(_event("ev2", "tool_result", 1, tool="refund_order",
                              payload={"output": {"amount": amount}})),
            json.dumps(_event("ev3", "llm_response", 2)),
        ]
    return [line + "\n" for line in lines]


def _write_trace(amount: float) -> None:
    if MODE == "no_trace":
        return
    if MODE == "empty_trace":
        TRACE_PATH.write_text("")
        return
    if MODE == "garbage_trace":
        TRACE_PATH.write_text("this is not json\n{'also': not}\n")
        return
    TRACE_PATH.write_text("".join(_trace_lines(amount)))


def _write_result(payload: dict) -> None:
    tmp = OUTPUT_DIR / "result.json.tmp"
    final = OUTPUT_DIR / "result.json"
    tmp.write_text(json.dumps(payload, sort_keys=True))
    with tmp.open("r+b") as handle:
        os.fsync(handle.fileno())
    tmp.rename(final)


def _echoed_manifest() -> dict:
    m = _manifest()
    return {
        "manifest_run_id": m.get("run_id"),
        "manifest_case_id": m.get("case_id"),
        "manifest_attempt_id": m.get("attempt_id"),
        "manifest_repeat_index": m.get("repeat_index"),
        "manifest_attempt": m.get("attempt"),
    }


def main() -> int:
    if MODE == "sleep":
        time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP_SECONDS", "30")))
        return 0
    if MODE == "leak":
        LEAK.append("invoked")
        _write_result(
            {"status": "completed", "amount": 49.99, "leak_len": len(LEAK), "case_id": CASE_ID}
        )
        _write_trace(49.99)
        return 0
    if MODE == "fail_exit":
        sys.stderr.write("fake agent failing on purpose\n")
        return 3
    if MODE == "missing_result":
        return 0
    if MODE == "bad_result":
        (OUTPUT_DIR / "result.json").write_text("{not json!")
        return 0
    amount = _amount()
    _write_trace(amount)
    if MODE == "provider_unreachable":
        _write_result(
            {
                "status": "error",
                "error": {"type": "provider_unreachable", "message": "model API down", "retryable": True},
                **_echoed_manifest(),
            }
        )
        return 0
    if MODE == "error_result":
        _write_result(
            {
                "status": "error",
                "error": {"type": "agent_bug", "message": "internal failure", "retryable": False},
                **_echoed_manifest(),
            }
        )
        return 0
    _write_result(
        {
            "status": "completed",
            "amount": amount,
            **_echoed_manifest(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
