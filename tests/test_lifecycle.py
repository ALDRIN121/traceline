"""Tests for the lifecycle state machines (src/llm_agent_eval/lifecycle.py).

Verifies the exact status values taken from the design (§11A/§13A.2/§18 DDL
for runs, cases, attempts; §32A for the smoke gate), the legal-transition
enforcement, and the helper predicates. The section files are authoritative —
these tests pin their exact spellings so a rename is a loud, deliberate act.
"""

from __future__ import annotations

import pytest

from llm_agent_eval.lifecycle import (
    ATTEMPT_TRANSITIONS,
    RUN_CASE_TRANSITIONS,
    RUN_TRANSITIONS,
    SMOKE_FAILURE_STATES,
    SMOKE_TRANSITIONS,
    AttemptStatus,
    IllegalTransition,
    RunCaseStatus,
    RunStatus,
    SmokeFailureState,
    SmokeState,
    is_attempt_terminal,
    is_run_terminal,
    is_smoke_failure,
    transition,
)


class TestExactValues:
    """§18 DDL / §11A / §32A spellings, verbatim."""

    def test_run_status_values(self):
        assert [s.value for s in RunStatus] == [
            "draft", "queued", "provisioning", "running", "aggregating",
            "complete", "failed", "cancelled", "incomplete",
        ]

    def test_case_status_values(self):
        assert [s.value for s in RunCaseStatus] == [
            "queued", "running", "completed", "failed", "cancelled", "skipped",
        ]

    def test_attempt_status_values(self):
        assert [s.value for s in AttemptStatus] == [
            "queued", "running", "completed", "timed_out", "budget_exceeded",
            "cancelled", "orphaned", "errored",
        ]

    def test_smoke_five_state_lifecycle_values(self):
        assert [SmokeState(s) for s in ("INGESTED", "ANALYZED", "RUNTIME_PREPARED",
                                        "SMOKE_PASSED", "EVALUABLE")] == [
            SmokeState.INGESTED, SmokeState.ANALYZED, SmokeState.RUNTIME_PREPARED,
            SmokeState.SMOKE_PASSED, SmokeState.EVALUABLE,
        ]

    def test_smoke_failure_states_verbatim(self):
        # §32A.4's canonical block, spelled exactly: lowercase ids.
        assert [s.value for s in SmokeFailureState] == [
            "entrypoint_missing", "install_failed", "invocation_failed",
            "provider_unreachable", "no_trace", "timeout",
        ]
        assert SMOKE_FAILURE_STATES == tuple(
            SmokeState(s) for s in SmokeFailureState
        )
        assert len(SMOKE_FAILURE_STATES) == 6

    def test_incomplete_is_not_terminal(self):
        # §11B.8: a status value, never a final state.
        assert is_run_terminal(RunStatus.INCOMPLETE) is False


class TestRunTransitions:
    def test_full_lifecycle(self):
        current = RunStatus.DRAFT
        for target in (
            RunStatus.QUEUED, RunStatus.PROVISIONING, RunStatus.RUNNING,
            RunStatus.AGGREGATING, RunStatus.COMPLETE,
        ):
            current = transition(current, target, RUN_TRANSITIONS)
        assert current is RunStatus.COMPLETE

    def test_illegal_jump_rejected(self):
        with pytest.raises(IllegalTransition):
            transition(RunStatus.DRAFT, RunStatus.RUNNING, RUN_TRANSITIONS)

    def test_terminal_run_has_no_outgoing_edges(self):
        for status in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED):
            with pytest.raises(IllegalTransition):
                transition(status, RunStatus.DRAFT, RUN_TRANSITIONS)
            with pytest.raises(IllegalTransition):
                transition(status, RunStatus.INCOMPLETE, RUN_TRANSITIONS)

    def test_same_status_is_a_legal_noop(self):
        assert transition(RunStatus.RUNNING, "running", RUN_TRANSITIONS) is RunStatus.RUNNING

    def test_incomplete_has_no_outgoing_edges(self):
        # A marker: a resumed run is driven by the caller, not by re-entry.
        with pytest.raises(IllegalTransition):
            transition(RunStatus.INCOMPLETE, RunStatus.RUNNING, RUN_TRANSITIONS)

    def test_unknown_status_rejected(self):
        with pytest.raises(IllegalTransition):
            transition(RunStatus.DRAFT, "green", RUN_TRANSITIONS)


class TestCaseAndAttemptTransitions:
    def test_case_lifecycle(self):
        assert transition(RunCaseStatus.QUEUED, "running", RUN_CASE_TRANSITIONS) is RunCaseStatus.RUNNING
        assert transition(RunCaseStatus.RUNNING, "completed", RUN_CASE_TRANSITIONS) is RunCaseStatus.COMPLETED

    def test_case_illegal_jump(self):
        with pytest.raises(IllegalTransition):
            transition(RunCaseStatus.QUEUED, RunCaseStatus.COMPLETED, RUN_CASE_TRANSITIONS)
        # A completed case does not re-open.
        with pytest.raises(IllegalTransition):
            transition(RunCaseStatus.COMPLETED, RunCaseStatus.RUNNING, RUN_CASE_TRANSITIONS)

    def test_attempt_illegal_jump(self):
        with pytest.raises(IllegalTransition):
            transition(AttemptStatus.QUEUED, AttemptStatus.COMPLETED, ATTEMPT_TRANSITIONS)

    def test_attempt_terminal_statuses(self):
        for status in AttemptStatus:
            assert is_attempt_terminal(status) == (
                status in (AttemptStatus.COMPLETED, AttemptStatus.TIMED_OUT,
                           AttemptStatus.BUDGET_EXCEEDED, AttemptStatus.CANCELLED,
                           AttemptStatus.ORPHANED, AttemptStatus.ERRORED)
            )

    def test_string_coercion(self):
        assert transition("queued", "running", ATTEMPT_TRANSITIONS) is AttemptStatus.RUNNING


class TestSmokeTransitions:
    def test_five_state_lifecycle(self):
        current = SmokeState.INGESTED
        for target in (SmokeState.ANALYZED, SmokeState.RUNTIME_PREPARED,
                       SmokeState.SMOKE_PASSED, SmokeState.EVALUABLE):
            current = transition(current, target, SMOKE_TRANSITIONS)
        assert current is SmokeState.EVALUABLE

    def test_failure_states_are_reachable(self):
        # §32A.4: any failure state from ANALYZED or RUNTIME_PREPARED.
        for failure in SMOKE_FAILURE_STATES:
            assert failure in SMOKE_TRANSITIONS[SmokeState.RUNTIME_PREPARED]
            assert failure in SMOKE_TRANSITIONS[SmokeState.ANALYZED]

    def test_recovery_paths(self):
        # §32A.4 recovery: re-prepare or re-smoke from any failure state.
        for failure in SMOKE_FAILURE_STATES:
            assert SmokeState.RUNTIME_PREPARED in SMOKE_TRANSITIONS[failure]
            assert SmokeState.SMOKE_PASSED in SMOKE_TRANSITIONS[failure]

    def test_evaluable_is_terminal(self):
        with pytest.raises(IllegalTransition):
            transition(SmokeState.EVALUABLE, SmokeState.RUNTIME_PREPARED, SMOKE_TRANSITIONS)
        with pytest.raises(IllegalTransition):
            transition(SmokeState.EVALUABLE, SmokeState.SMOKE_PASSED, SMOKE_TRANSITIONS)

    def test_ingested_cannot_fail_smoke(self):
        # A project that has not been runtime-prepared cannot fail smoke (§32A).
        for failure in SMOKE_FAILURE_STATES:
            with pytest.raises(IllegalTransition):
                transition(SmokeState.INGESTED, failure, SMOKE_TRANSITIONS)

    def test_smoke_failure_predicate(self):
        assert is_smoke_failure(SmokeState.NO_TRACE)
        assert not is_smoke_failure(SmokeState.SMOKE_PASSED)
        assert not is_smoke_failure(SmokeState.EVALUABLE)

    def test_failure_to_other_failure_is_legal(self):
        # A re-smoke can fail into a different failure state.
        assert SmokeState.TIMEOUT in SMOKE_TRANSITIONS[SmokeState.NO_TRACE]
