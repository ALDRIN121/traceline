"""Tests for the invocation layer (src/llm_agent_eval/runner.py).

Covers the §11A protocol from the runner's side: fresh process per attempt,
the input/manifest file contract (manifest never carries `expected`), the
exit-code contract, the timeout kill (SIGTERM -> grace -> SIGKILL), output
caps, and the full classification decision table — including the real states
`no_trace` and `provider_unreachable`, and the rule that a killed attempt is
never success.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from llm_agent_eval.runner import (
    COMPLETED,
    INVOCATION_FAILED,
    NO_TRACE,
    PROVIDER_UNREACHABLE,
    TIMED_OUT,
    invoke_agent,
)
from llm_agent_eval.spec import EvaluationSpec, validate_spec

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = [sys.executable, str(FIXTURES / "fake_agent.py")]

# Built via validate_spec (not by importing TestCase, which pytest would try
# to collect as a test class).
SPEC = validate_spec(
    {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "cases": [
            {
                "case_id": "c1", "name": "one", "description": "first",
                "input": {"order_id": "123"},
            }
        ],
        "metrics": [],
    }
)
CASE = SPEC.cases[0]


def invoke(tmp_path, *, mode="ok", timeout=30.0, env_extra=None, **kwargs):
    env = {"FAKE_AGENT_MODE": mode}
    if env_extra:
        env.update(env_extra)
    return invoke_agent(
        agent_command=AGENT,
        run_id="run1",
        workspace_id="ws1",
        case=CASE,
        attempt_id="a1",
        repeat_index=0,
        attempt=0,
        spec=SPEC,
        timeout_seconds=timeout,
        work_dir=tmp_path / "work",
        env_extra=env,
        **kwargs,
    )


class TestHappyPath:
    def test_completed_with_events_and_echoed_manifest(self, tmp_path):
        result = invoke(tmp_path)
        assert result.status == COMPLETED
        assert result.exit_code == 0
        assert len(result.trace_events) == 3
        assert [e.type.value for e in result.trace_events] == [
            "tool_call", "tool_result", "llm_response",
        ]
        assert result.trace_events[1].tool == "refund_order"
        # The agent read the manifest and echoed it — the protocol delivered.
        payload = result.result_payload
        assert payload["manifest_run_id"] == "run1"
        assert payload["manifest_case_id"] == "c1"
        assert payload["manifest_attempt_id"] == "a1"
        assert payload["manifest_repeat_index"] == 0
        assert payload["manifest_attempt"] == 0
        assert result.error is None

    def test_case_json_carries_input_never_expected(self, tmp_path):
        invoke(tmp_path)
        case_json = json.loads(
            (tmp_path / "work" / "input" / "case.json").read_text()
        )
        assert case_json["case_id"] == "c1"
        assert case_json["input"] == {"order_id": "123"}
        assert "expected" not in case_json

    def test_manifest_never_carries_expected(self, tmp_path):
        # §11A.3: the agent must not be able to read its own answer key.
        invoke(tmp_path)
        manifest = json.loads(
            (tmp_path / "work" / "input" / "manifest.json").read_text()
        )
        assert manifest["run_id"] == "run1"
        assert manifest["spec_name"] == "refund-suite"
        assert "expected" not in manifest

    def test_events_claim_the_invocation_identity(self, tmp_path):
        result = invoke(tmp_path)
        for event in result.trace_events:
            assert event.run_id == "run1"
            assert event.case_id == "c1"
            assert event.attempt_id == "a1"


class TestFailureStates:
    @pytest.mark.parametrize("mode,status,exit_code", [
        ("fail_exit", INVOCATION_FAILED, 3),
        ("missing_result", INVOCATION_FAILED, 0),
        ("bad_result", INVOCATION_FAILED, 0),
        ("error_result", INVOCATION_FAILED, 0),
        ("garbage_trace", INVOCATION_FAILED, 0),
    ])
    def test_invocation_failed_family(self, tmp_path, mode, status, exit_code):
        result = invoke(tmp_path, mode=mode)
        assert result.status == status
        assert result.exit_code == exit_code
        assert result.error is not None
        assert result.trace_events == ()

    def test_provider_unreachable_is_its_own_state(self, tmp_path):
        # §11A.6: the agent ran but its model provider was unreachable — a
        # distinct lifecycle state, never a generic invocation failure.
        result = invoke(tmp_path, mode="provider_unreachable")
        assert result.status == PROVIDER_UNREACHABLE
        assert result.exit_code == 0
        assert result.result_payload["error"]["type"] == "provider_unreachable"

    def test_no_trace_is_a_real_state(self, tmp_path):
        result = invoke(tmp_path, mode="no_trace")
        assert result.status == NO_TRACE
        assert result.exit_code == 0  # a clean exit with no evidence
        assert result.trace_events == ()

    def test_empty_trace_is_no_trace(self, tmp_path):
        result = invoke(tmp_path, mode="empty_trace")
        assert result.status == NO_TRACE

    def test_nonzero_exit_never_scored_as_assertion(self, tmp_path):
        result = invoke(tmp_path, mode="fail_exit")
        assert result.status == INVOCATION_FAILED
        assert result.trace_events == ()  # no evidence from a failed run


class TestTimeout:
    def test_timeout_kills_and_is_never_success(self, tmp_path):
        result = invoke(
            tmp_path, mode="sleep", timeout=0.4,
            env_extra={"FAKE_AGENT_SLEEP_SECONDS": "30"},
        )
        assert result.status == TIMED_OUT
        assert "timed out" in result.error
        assert result.trace_events == ()
        assert result.exit_code is not None  # the kill's exit, not the agent's

    def test_empty_command_is_invocation_failed(self, tmp_path):
        result = invoke_agent(
            agent_command=[],
            run_id="run1", workspace_id="ws1", case=CASE,
            attempt_id="a1", repeat_index=0, attempt=0, spec=SPEC,
            timeout_seconds=30.0, work_dir=tmp_path / "work",
        )
        assert result.status == INVOCATION_FAILED
        assert result.error is not None


class TestOutputCaps:
    def test_stdout_and_stderr_tails_are_captured(self, tmp_path):
        result = invoke(tmp_path, mode="fail_exit")
        assert "failing on purpose" in result.stderr_tail
