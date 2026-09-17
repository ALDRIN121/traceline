"""Tests for the execution spine (src/llm_agent_eval/engine.py).

Covers the smoke gate (§32A: five-state lifecycle, all six failure states,
recovery), run creation (tier caps §11B.9, idempotency, the EVALUABLE project
gate), and the run engine: fresh process per case, first-attempt authority
(§11C.3), repeats vs retries, no_trace as a real state scored by on_missing,
the redaction/identity capture contract, price-version stamping, the 1,000
event cap, aggregation states (§13A.4), gates, resume re-scoring from stored
traces (§13A.1.3), and the incomplete marker (§11B.8).
"""

from __future__ import annotations

import json
import shlex
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import llm_agent_eval.engine as engine_module
from llm_agent_eval.engine import (
    Engine,
    EngineError,
    ProjectNotEvaluableError,
    RunAlreadyTerminalError,
    TierViolationError,
)
from llm_agent_eval.judge import ScriptedJudge
from llm_agent_eval.lifecycle import (
    AttemptStatus,
    RunCaseStatus,
    RunStatus,
    SmokeState,
)
from llm_agent_eval.spec import JudgeBinding
from llm_agent_eval.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = [sys.executable, str(FIXTURES / "fake_agent.py")]
WORKSPACE = "ws1"


@contextmanager
def agent_env(env: dict):
    """Patch the engine's invoke_agent to merge extra env for the fake agent
    (modes + amount knobs), restoring after. Patched on the engine module —
    the engine holds its own reference to invoke_agent, so patching the
    runner module would be a no-op."""
    original = engine_module.invoke_agent

    def invoke_with_env(**kwargs):
        merged = dict(env)
        merged.update(kwargs.get("env_extra") or {})
        return original(**kwargs, env_extra=merged)

    engine_module.invoke_agent = invoke_with_env
    try:
        yield
    finally:
        engine_module.invoke_agent = original


def make_spec(n_cases=2, *, metric_overrides=None):
    cases = [
        {"case_id": f"c{i}", "name": f"case {i}", "input": {"order_id": str(i)}}
        for i in range(n_cases)
    ]
    metric = {
        "metric_id": "refund_amount",
        "name": "refund amount correct",
        "type": "scalar",
        "target": {
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        "evaluator": {"type": "numeric", "expected": 49.99, "tolerance": 0},
        "scoring": {"type": "binary", "range": [0, 1],
                    "condition": "raw_value == 49.99"},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "gate": {"min": 1.0},
        "provisional": False,
    }
    if metric_overrides:
        metric.update(metric_overrides)
    return {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "cases": cases,
        "metrics": [metric],
    }


@pytest.fixture()
def storage(tmp_path):
    db = Storage(tmp_path / "eval.db")
    db.create_schema()
    yield db
    db.close()


@pytest.fixture()
def engine(storage, tmp_path):
    return Engine(storage, work_root=tmp_path / "work")


def make_project(engine, *, mode_env=None, timeout_seconds=30.0):
    """A project with the smoke gate passed (SMOKE_PASSED)."""
    project = engine.create_project(WORKSPACE, "refund-agent")
    project = engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
    assert project.smoke_state == SmokeState.RUNTIME_PREPARED.value
    with agent_env(mode_env or {}):
        attempt = engine.smoke(project.project_id, WORKSPACE,
                               timeout_seconds=timeout_seconds)
    return project, attempt


def make_evaluable_project(engine, **kwargs):
    project, attempt = make_project(engine, **kwargs)
    if attempt.state != SmokeState.SMOKE_PASSED.value:
        pytest.fail(f"smoke did not pass: {attempt.state} {attempt.failure_detail}")
    project = engine.advance_project(project.project_id, WORKSPACE, SmokeState.EVALUABLE)
    assert project.smoke_state == SmokeState.EVALUABLE.value
    return project


def create_run(engine, project, spec=None, **kwargs):
    return engine.create_run(
        WORKSPACE,
        spec or make_spec(),
        project_id=project.project_id,
        tier="quick",
        repeats=1,
        retry_max=0,
        entrypoint=AGENT,
        **kwargs,
    )


def run_with_env(engine, run_id, env):
    with agent_env(env):
        return engine.run(run_id, WORKSPACE)


class TestSmokeGate:
    def test_project_starts_ingested(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        assert project.smoke_state == SmokeState.INGESTED.value

    def test_prepare_project_full_journey(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        prepared = engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
        assert prepared.smoke_state == SmokeState.RUNTIME_PREPARED.value
        assert prepared.entrypoint == tuple(AGENT)

    def test_prepare_project_empty_entrypoint(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        prepared = engine.prepare_project(project.project_id, WORKSPACE, entrypoint=())
        assert prepared.smoke_state == SmokeState.ENTRYPOINT_MISSING.value

    def test_recovery_from_failure_state(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        engine.prepare_project(project.project_id, WORKSPACE, entrypoint=())
        recovered = engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
        assert recovered.smoke_state == SmokeState.RUNTIME_PREPARED.value

    def test_prepare_after_smoke_passed_is_refused(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
        engine.smoke(project.project_id, WORKSPACE)
        with pytest.raises(EngineError):
            engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)

    def test_smoke_passes_and_records_evidence(self, engine):
        project, attempt = make_project(engine)
        assert attempt.state == SmokeState.SMOKE_PASSED.value
        assert attempt.exit_code == 0
        assert attempt.reproduce_locally == shlex.join(AGENT)
        assert attempt.trace_path is not None and Path(attempt.trace_path).exists()
        project = engine.get_project(project.project_id, WORKSPACE)
        assert project.smoke_state == SmokeState.SMOKE_PASSED.value

    @pytest.mark.parametrize("mode,expected", [
        ("no_trace", SmokeState.NO_TRACE),
        ("provider_unreachable", SmokeState.PROVIDER_UNREACHABLE),
        ("fail_exit", SmokeState.INVOCATION_FAILED),
        ("error_result", SmokeState.INVOCATION_FAILED),
    ])
    def test_smoke_failure_states(self, engine, mode, expected):
        project, attempt = make_project(engine, mode_env={"FAKE_AGENT_MODE": mode})
        assert attempt.state == expected.value
        assert attempt.failure_detail is not None
        project = engine.get_project(project.project_id, WORKSPACE)
        assert project.smoke_state == expected.value
        assert project.smoke_failure_detail == attempt.failure_detail

    def test_smoke_timeout_state(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
        with agent_env({"FAKE_AGENT_MODE": "sleep", "FAKE_AGENT_SLEEP_SECONDS": "30"}):
            attempt = engine.smoke(project.project_id, WORKSPACE, timeout_seconds=0.4)
        assert attempt.state == SmokeState.TIMEOUT.value

    def test_smoke_recovery_after_failure(self, engine):
        project, attempt = make_project(engine, mode_env={"FAKE_AGENT_MODE": "no_trace"})
        assert attempt.state == SmokeState.NO_TRACE.value
        # Recovery (§32A.4): a re-smoke from the failure state can pass.
        attempt = engine.smoke(project.project_id, WORKSPACE)
        assert attempt.state == SmokeState.SMOKE_PASSED.value
        history = engine.storage.list_smoke_attempts(project.project_id, WORKSPACE)
        assert [a.state for a in history] == ["SMOKE_PASSED", "no_trace"]

    def test_smoke_refused_before_runtime_prepared(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        with pytest.raises(EngineError):
            engine.smoke(project.project_id, WORKSPACE)

    def test_smoke_requires_entrypoint(self, engine):
        project = engine.create_project(WORKSPACE, "refund-agent")
        engine.prepare_project(project.project_id, WORKSPACE, entrypoint=())
        with pytest.raises(EngineError):
            engine.smoke(project.project_id, WORKSPACE)

    def test_smoke_to_evaluable(self, engine):
        project = make_evaluable_project(engine)
        assert project.smoke_state == SmokeState.EVALUABLE.value

    def test_advance_project_rejects_illegal_jump(self, engine):
        project, _ = make_project(engine)
        with pytest.raises(ValueError):
            engine.advance_project(project.project_id, WORKSPACE, SmokeState.RUNTIME_PREPARED)
        assert engine.get_project(project.project_id, WORKSPACE).smoke_state == "SMOKE_PASSED"


class TestCreateRunGate:
    def test_run_requires_evaluable_project(self, engine):
        project, _ = make_project(engine)
        with pytest.raises(ProjectNotEvaluableError):
            create_run(engine, project)
        with pytest.raises(ProjectNotEvaluableError):
            engine.create_run(WORKSPACE, make_spec(), project_id=project.project_id,
                              tier="quick", entrypoint=AGENT)

    def test_run_requires_entrypoint_without_project(self, engine):
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, make_spec(), tier="quick")

    def test_run_without_project_works(self, engine):
        run = engine.create_run(WORKSPACE, make_spec(), tier="quick", entrypoint=AGENT)
        assert run.status == RunStatus.DRAFT.value

    def test_zero_cases_rejected(self, engine):
        project = make_evaluable_project(engine)
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, make_spec(n_cases=0), project_id=project.project_id,
                              tier="quick", entrypoint=AGENT)

    def test_zero_metrics_rejected(self, engine):
        project = make_evaluable_project(engine)
        spec = make_spec()
        spec["metrics"] = []
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, spec, project_id=project.project_id,
                              tier="quick", entrypoint=AGENT)

    def test_concurrency_must_be_one(self, engine):
        project = make_evaluable_project(engine)
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, make_spec(), project_id=project.project_id,
                              tier="quick", entrypoint=AGENT, concurrency=4)

    def test_repeats_must_be_positive(self, engine):
        project = make_evaluable_project(engine)
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, make_spec(), project_id=project.project_id,
                              tier="quick", entrypoint=AGENT, repeats=0)

    def test_spec_validated_by_pydantic(self, engine):
        project = make_evaluable_project(engine)
        spec = make_spec()
        spec["metrics"][0]["target"]["on_missing"] = None  # §7A.3: never defaulted
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, spec, project_id=project.project_id,
                              tier="quick", entrypoint=AGENT)

    def test_run_tier_resolved_from_spec_hint(self, engine):
        project = make_evaluable_project(engine)
        spec = make_spec()
        spec["run_tier"] = "quick"
        run = engine.create_run(WORKSPACE, spec, project_id=project.project_id,
                                entrypoint=AGENT)
        assert run.tier == "quick"

    def test_tier_required_when_no_hint(self, engine):
        project = make_evaluable_project(engine)
        with pytest.raises(ValueError):
            engine.create_run(WORKSPACE, make_spec(), project_id=project.project_id,
                              entrypoint=AGENT)


class TestTierCaps:
    """§11B.9: quick ≤ 20, standard 100–200, full ≤ 5,000 (hard cap)."""

    @pytest.mark.parametrize("n_cases,expected_ok", [
        (20, True), (21, False),
    ])
    def test_quick_cap(self, engine, n_cases, expected_ok):
        project = make_evaluable_project(engine)
        if expected_ok:
            run = engine.create_run(WORKSPACE, make_spec(n_cases=n_cases),
                                    project_id=project.project_id, tier="quick",
                                    entrypoint=AGENT)
            assert run.case_count == n_cases
        else:
            with pytest.raises(TierViolationError):
                engine.create_run(WORKSPACE, make_spec(n_cases=n_cases),
                                  project_id=project.project_id, tier="quick",
                                  entrypoint=AGENT)

    @pytest.mark.parametrize("n_cases", [99, 201, 5001])
    def test_standard_cap_floor_and_ceiling(self, engine, n_cases):
        project = make_evaluable_project(engine)
        with pytest.raises(TierViolationError):
            engine.create_run(WORKSPACE, make_spec(n_cases=n_cases),
                              project_id=project.project_id, tier="standard",
                              entrypoint=AGENT)

    def test_standard_cap_within_bounds(self, engine):
        project = make_evaluable_project(engine)
        run = engine.create_run(WORKSPACE, make_spec(n_cases=100),
                                project_id=project.project_id, tier="standard",
                                entrypoint=AGENT)
        assert run.case_count == 100

    def test_full_hard_cap(self, engine):
        project = make_evaluable_project(engine)
        with pytest.raises(TierViolationError):
            engine.create_run(WORKSPACE, make_spec(n_cases=5001),
                              project_id=project.project_id, tier="full",
                              entrypoint=AGENT)


class TestCreateRunIdempotency:
    def test_same_key_returns_same_run(self, engine):
        project = make_evaluable_project(engine)
        spec = make_spec()
        run_a = engine.create_run(WORKSPACE, spec, project_id=project.project_id,
                                  tier="quick", entrypoint=AGENT, idempotency_key="k1")
        run_b = engine.create_run(WORKSPACE, spec, project_id=project.project_id,
                                  tier="quick", entrypoint=AGENT, idempotency_key="k1")
        assert run_a.run_id == run_b.run_id
        assert len(engine.storage.list_cases(run_a.run_id, WORKSPACE)) == 2


class TestRunHappyPath:
    def test_full_run_completes_and_aggregates(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        result = engine.run(run.run_id, WORKSPACE)
        assert result.status == RunStatus.COMPLETE.value
        assert len(result.case_results) == 2
        for case in result.case_results:
            assert case.status == RunCaseStatus.COMPLETED.value
            assert case.classification == "PASSING"
            assert len(case.repeat_outcomes) == 1
            assert case.repeat_outcomes[0].status == AttemptStatus.COMPLETED.value
            assert case.repeat_outcomes[0].event_count == 3
            assert case.retry_outcomes == ()
        metric = result.metric_results[0]
        assert metric.value == 1.0
        assert metric.aggregation_state == "COMPLETE"
        assert metric.sample_n == 2
        assert metric.error_n == 0
        assert metric.gate_status == "PASS"
        assert metric.no_ci is True  # repeats == 1 < 3 (§11C.4)
        assert len(result.gate_results) == 1
        assert result.gate_results[0].passed is True
        assert result.warnings == ()

    def test_run_lifecycle_and_timestamps(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        engine.run(run.run_id, WORKSPACE)
        run = engine.storage.get_run(run.run_id, WORKSPACE)
        assert run.run_started_at is not None
        assert run.run_completed_at is not None
        assert run.status == "complete"

    def test_terminal_run_refuses_to_rerun(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        engine.run(run.run_id, WORKSPACE)
        with pytest.raises(RunAlreadyTerminalError):
            engine.run(run.run_id, WORKSPACE)

    def test_traces_persisted_with_stamped_price_version(self, engine):
        # The fake agent writes price_version "1999-01"; the engine stamps the
        # config's price_version at ingestion (§12A.5).
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        engine.run(run.run_id, WORKSPACE)
        events = engine.storage.get_trace_events(run_id=run.run_id, workspace_id=WORKSPACE)
        assert len(events) == 6  # 2 cases x 3 events
        cost_events = [e for e in events if e.cost is not None]
        assert cost_events
        assert all(e.cost.price_version == "2026-08" for e in cost_events)

    def test_failing_amount_fails_case_and_gate(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        result = run_with_env(engine, run.run_id, {"FAKE_AGENT_OUTPUT_AMOUNT": "41"})
        for case in result.case_results:
            assert case.classification == "FAILING"
        metric = result.metric_results[0]
        assert metric.value == 0.0
        assert metric.gate_status == "FAIL"


class TestFreshProcessPerCase:
    def test_state_never_leaks_between_cases(self, engine):
        # The fake agent's module-global LEAK list: each case must observe an
        # empty list, proving each attempt is its own process (§11A invariant).
        project = make_evaluable_project(engine)
        run = create_run(engine, project, spec=make_spec(n_cases=2))
        result = run_with_env(engine, run.run_id, {"FAKE_AGENT_MODE": "leak"})
        for case in result.case_results:
            payload = case.repeat_outcomes[0].result_payload
            assert payload["leak_len"] == 1, "agent state leaked between cases"
            assert payload["case_id"] == case.case_id

    def test_case_identifier_cannot_escape_its_generated_attempt_directory(self, engine):
        project = make_evaluable_project(engine)
        spec = make_spec(n_cases=1)
        spec["cases"][0]["case_id"] = "../../outside"
        run = create_run(engine, project, spec=spec)
        result = run_with_env(engine, run.run_id, {})
        assert result.case_results[0].status == "completed"
        run_root = engine.work_root / "runs" / run.run_id
        assert all(path.resolve().is_relative_to(run_root.resolve()) for path in run_root.rglob("*"))
        assert not (engine.work_root / "outside").exists()


class TestRepeatsAndRetries:
    def _run(self, engine, project, *, repeats=1, retry_max=0, extra_env=None,
             timeout_seconds=120.0):
        run = engine.create_run(WORKSPACE, make_spec(), project_id=project.project_id,
                                tier="quick", entrypoint=AGENT,
                                repeats=repeats, retry_max=retry_max,
                                timeout_seconds=timeout_seconds)
        result = run_with_env(engine, run.run_id, extra_env or {})
        return run, result

    def test_repeats_are_independent_samples(self, engine):
        project = make_evaluable_project(engine)
        run, result = self._run(engine, project, repeats=3)
        for case in result.case_results:
            assert len(case.repeat_outcomes) == 3
            assert case.classification == "PASSING"
        metric = result.metric_results[0]
        assert metric.no_ci is False  # repeats >= 3 -> CI-capable (§11C.4)
        assert metric.value == 1.0

    def test_flaky_classification(self, engine):
        # Repeat 0 returns 41 (fails), repeats 1-2 return 49.99 -> FLAKY.
        project = make_evaluable_project(engine)
        run, result = self._run(
            engine, project, repeats=3,
            extra_env={"FAKE_AGENT_OUTPUT_AMOUNT_REPEAT_0": "41"},
        )
        for case in result.case_results:
            assert case.classification == "FLAKY"  # 0 < pass_rate < 1 (§11C.3)
        metric = result.metric_results[0]
        assert metric.value == pytest.approx(2 / 3)

    def test_failing_classification(self, engine):
        project = make_evaluable_project(engine)
        run, result = self._run(
            engine, project, repeats=2,
            extra_env={"FAKE_AGENT_OUTPUT_AMOUNT": "41"},
        )
        for case in result.case_results:
            assert case.classification == "FAILING"

    def test_first_attempt_is_authoritative(self, engine):
        # Attempt 0 fails (41); the retry passes (49.99). The retry is stored
        # flagged on_retry_override and never re-enters the pass rate (§11C.3).
        project = make_evaluable_project(engine)
        run, result = self._run(
            engine, project, retry_max=1,
            extra_env={"FAKE_AGENT_OUTPUT_AMOUNT_ATTEMPT_0": "41"},
        )
        case = result.case_results[0]
        assert len(case.repeat_outcomes) == 1
        assert len(case.retry_outcomes) == 1
        assert case.repeat_outcomes[0].scores[0].status.value == "FAIL"
        assert case.retry_outcomes[0].scores[0].status.value == "PASS"
        # §11C.3: a case that passes only on retry is FAILED on the first
        # attempt — classification comes from first-attempt outcomes only.
        assert case.classification == "FAILING"
        metric = result.metric_results[0]
        assert metric.value == 0.0
        rows = engine.storage.get_case_scores(run_id=run.run_id, workspace_id=WORKSPACE)
        # §18.6: rows land under the current score_revision — last attempt
        # wins — so each case's single row IS the retry row, flagged
        # on_retry_override (authoritative first-attempt results live in the
        # aggregation, computed in-memory from attempt-0 scores).
        assert len(rows) == 2  # one row per case
        retry_rows = [r for r in rows if r.on_retry_override]
        assert len(retry_rows) == 2
        assert all(r.status == "PASS" for r in retry_rows)
        assert all(r.is_authoritative is False for r in retry_rows)
        assert all(r.score == 1.0 for r in retry_rows)

    def test_no_retry_when_first_attempt_passes(self, engine):
        project = make_evaluable_project(engine)
        run, result = self._run(engine, project, retry_max=3)
        for case in result.case_results:
            assert len(case.repeat_outcomes) == 1
            assert case.retry_outcomes == ()

    def test_no_retry_after_timeout(self, engine):
        # Repeating an infra failure masks it (§11C.3) — retries run only
        # after a completed FAIL, never after a timeout.
        project = make_evaluable_project(engine)
        run, result = self._run(
            engine, project, retry_max=2, timeout_seconds=0.4,
            extra_env={"FAKE_AGENT_MODE": "sleep", "FAKE_AGENT_SLEEP_SECONDS": "30"},
        )
        for case in result.case_results:
            assert len(case.repeat_outcomes) == 1
            assert case.repeat_outcomes[0].status == AttemptStatus.TIMED_OUT.value
            assert case.retry_outcomes == ()
            assert case.status == RunCaseStatus.FAILED.value
            assert case.classification == "ERRORED"
            assert case.error_category == "infra"


class TestNoTrace:
    def test_no_trace_scored_via_on_missing_never_fabricated(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        result = run_with_env(engine, run.run_id, {"FAKE_AGENT_MODE": "no_trace"})
        case = result.case_results[0]
        # The attempt completed; the empty trace is scored by on_missing (fail).
        assert case.repeat_outcomes[0].status == AttemptStatus.COMPLETED.value
        assert case.repeat_outcomes[0].event_count == 0
        assert case.repeat_outcomes[0].scores[0].status.value == "FAIL"
        assert case.classification == "FAILING"
        metric = result.metric_results[0]
        assert metric.value == 0.0
        assert any("no trace" in w for w in result.warnings)


class TestCaptureContract:
    def _run_with_mode(self, engine, mode):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        result = run_with_env(engine, run.run_id, {"FAKE_AGENT_MODE": mode})
        return run, result

    def test_event_without_redaction_state_is_refused(self, engine):
        # §12A.6: redaction happens at capture — the engine verifies the
        # contract and refuses unverified events.
        run, result = self._run_with_mode(engine, "no_redaction_state")
        case = result.case_results[0]
        assert case.repeat_outcomes[0].status == AttemptStatus.ERRORED.value
        assert "redaction_state" in case.repeat_outcomes[0].error
        assert case.status == RunCaseStatus.FAILED.value
        assert case.error_category == "agent"
        # The offending events were never stored.
        assert engine.storage.get_trace_events(run_id=run.run_id, workspace_id=WORKSPACE) == []
        # The run itself completes (PARTIAL — one case missing).
        assert result.status == RunStatus.COMPLETE.value
        assert result.metric_results[0].aggregation_state == "PARTIAL"

    def test_wrong_identity_claims_are_refused(self, engine):
        run, result = self._run_with_mode(engine, "wrong_ids")
        case = result.case_results[0]
        assert case.repeat_outcomes[0].status == AttemptStatus.ERRORED.value
        assert "contamination" in case.repeat_outcomes[0].error

    def test_event_cap_truncates_and_marks(self, engine):
        # §18 Q8: 1,000-event per-attempt cap; overflow is marked, never
        # partially trusted.
        run, result = self._run_with_mode(engine, "huge_trace")
        assert len(result.case_results) == 2
        for case in result.case_results:
            attempt = case.repeat_outcomes[0]
            assert attempt.event_count == 1000
            assert attempt.trace_truncated is True
            # The huge trace has no tool_result with the amount payload —
            # the scalar metric finds zero matches and on_missing (fail) rules.
            assert attempt.scores[0].status.value == "FAIL"
        stored = engine.storage.get_trace_events(run_id=run.run_id, workspace_id=WORKSPACE)
        assert len(stored) == 2000  # 1000 per case, overflow dropped at capture


class TestResume:
    def test_resume_rescores_finished_cases_from_traces(self, engine):
        # §13A.1.3: a run interrupted mid-way re-scores finished cases from
        # stored traces — no agent re-run — and aggregates over ALL cases.
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        first = engine.run_case(run.run_id, "c1", WORKSPACE)
        assert first.status == RunCaseStatus.COMPLETED.value
        result = engine.run(run.run_id, WORKSPACE)
        assert result.status == RunStatus.COMPLETE.value
        assert len(result.case_results) == 1  # only c2 executed in this pass
        metric = result.metric_results[0]
        assert metric.value == 1.0
        assert metric.aggregation_state == "COMPLETE"
        assert metric.sample_n == 2  # c1 (re-scored) + c2 (executed)
        # c1's attempts were NOT re-invoked: still exactly one attempt row.
        attempts = engine.storage.list_attempts(run.run_id, WORKSPACE, case_id="c1")
        assert len(attempts) == 1

    def test_resume_orphans_stale_attempts_and_cancels_stuck_cases(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        # Simulate a crashed runner: an attempt left RUNNING, a case left
        # RUNNING, the run itself left RUNNING.
        attempt = engine.storage.create_attempt(run_id=run.run_id, case_id="c1",
                                                workspace_id=WORKSPACE,
                                                repeat_index=0, attempt=0)
        engine.storage.set_attempt_status(attempt.attempt_id, WORKSPACE,
                                          AttemptStatus.RUNNING)
        engine.storage.set_case_status(run.run_id, "c1", WORKSPACE, RunCaseStatus.RUNNING)
        for status in (RunStatus.QUEUED, RunStatus.PROVISIONING, RunStatus.RUNNING):
            engine.storage.set_run_status(run.run_id, WORKSPACE, status)
        result = engine.run(run.run_id, WORKSPACE)
        assert result.status == RunStatus.COMPLETE.value
        orphan = engine.storage.get_attempt_by_id(attempt.attempt_id, WORKSPACE)
        assert orphan.status == AttemptStatus.ORPHANED.value
        # The stuck case was cancelled and re-run fresh.
        attempts = engine.storage.list_attempts(run.run_id, WORKSPACE, case_id="c1")
        assert len(attempts) == 2  # orphaned + fresh
        fresh = [a for a in attempts if a.status == AttemptStatus.COMPLETED.value]
        assert len(fresh) == 1

    def test_engine_crash_marks_run_incomplete(self, engine, monkeypatch):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)

        def boom(**kwargs):
            raise RuntimeError("simulated engine crash")

        monkeypatch.setattr(engine_module, "invoke_agent", boom)
        with pytest.raises(RuntimeError):
            engine.run(run.run_id, WORKSPACE)
        # §11B.8: a marker, never a final state.
        run = engine.storage.get_run(run.run_id, WORKSPACE)
        assert run.status == RunStatus.INCOMPLETE.value
        # The crashed case is left RUNNING as evidence. The loop hits the
        # spec's first case (c0) before c1, so the crash lands on c0.
        case = engine.storage.get_case(run.run_id, "c0", WORKSPACE)
        assert case.status == RunCaseStatus.RUNNING.value
        attempt = engine.storage.list_attempts(run.run_id, WORKSPACE, case_id="c0")[0]
        assert attempt.status == AttemptStatus.ERRORED.value
        assert "runner crashed" in attempt.error

    def test_incomplete_run_resumes_to_complete(self, engine, monkeypatch):
        # §11B.8: INCOMPLETE is a marker, never a final state — the explicit
        # resume requeues the run, re-runs the interrupted case fresh, and
        # lands COMPLETE with a score for every case.
        project = make_evaluable_project(engine)
        run = create_run(engine, project)

        def boom(**kwargs):
            raise RuntimeError("simulated engine crash")

        monkeypatch.setattr(engine_module, "invoke_agent", boom)
        with pytest.raises(RuntimeError):
            engine.run(run.run_id, WORKSPACE)
        run = engine.storage.get_run(run.run_id, WORKSPACE)
        assert run.status == RunStatus.INCOMPLETE.value

        monkeypatch.undo()  # the crash is over; the resumed run must execute
        result = engine.run(run.run_id, WORKSPACE)
        assert result.status == RunStatus.COMPLETE.value
        assert len(result.case_results) == 2  # both cases executed in the resumed pass
        metric = result.metric_results[0]
        assert metric.value == 1.0
        assert metric.aggregation_state == "COMPLETE"
        assert metric.sample_n == 2  # a score for every case
        # The interrupted case ran fresh: the crashed attempt remains as
        # evidence (errored, never silently dropped) beside the completed one.
        attempts = engine.storage.list_attempts(run.run_id, WORKSPACE, case_id="c0")
        assert {a.status for a in attempts} == {
            AttemptStatus.ERRORED.value, AttemptStatus.COMPLETED.value,
        }

    def test_incomplete_resume_requeues_failed_case(self, engine, monkeypatch):
        # §35B.3: on resume, failed cases re-run fresh — they are never
        # re-scored from the failed attempt. This exercises the INCOMPLETE
        # clause of the FAILED-requeue condition: after the INCOMPLETE→QUEUED
        # transition the run status is re-read from storage, so the clause
        # must match INCOMPLETE or the failed case is silently re-scored from
        # its rejected (empty) trace and stays failed.
        project = make_evaluable_project(engine)
        run = create_run(engine, project)

        orig = engine_module.invoke_agent

        def boom_on_c1(**kwargs):
            if kwargs["case"].case_id == "c1":
                raise RuntimeError("simulated engine crash on c1")
            return orig(**kwargs)

        monkeypatch.setattr(engine_module, "invoke_agent", boom_on_c1)
        # c0 fails the redaction contract (attempt ERRORED → case FAILED), the
        # engine proceeds to c1, and the crash aborts the run there.
        with agent_env({"FAKE_AGENT_MODE": "no_redaction_state"}):
            with pytest.raises(RuntimeError):
                engine.run(run.run_id, WORKSPACE)
        monkeypatch.undo()  # the crash is over; the resumed run must execute
        run = engine.storage.get_run(run.run_id, WORKSPACE)
        assert run.status == RunStatus.INCOMPLETE.value
        case = engine.storage.get_case(run.run_id, "c0", WORKSPACE)
        assert case.status == RunCaseStatus.FAILED.value  # failed before the crash

        result = engine.run(run.run_id, WORKSPACE)  # resume, clean env
        assert result.status == RunStatus.COMPLETE.value
        assert all(c.status == "completed" for c in result.case_results)
        metric = result.metric_results[0]
        assert metric.value == 1.0
        assert metric.sample_n == 2  # the failed case re-ran fresh, not re-scored
        attempts = engine.storage.list_attempts(run.run_id, WORKSPACE, case_id="c0")
        assert {a.status for a in attempts} == {
            AttemptStatus.ERRORED.value, AttemptStatus.COMPLETED.value,
        }

    def test_incomplete_run_can_be_cancelled(self, engine, monkeypatch):
        # The INCOMPLETE → CANCELLED arc (§11B.8): a crashed run with no live
        # worker is cancelled directly — what /cancel dispatches.
        project = make_evaluable_project(engine)
        run = create_run(engine, project)

        def boom(**kwargs):
            raise RuntimeError("simulated engine crash")

        monkeypatch.setattr(engine_module, "invoke_agent", boom)
        with pytest.raises(RuntimeError):
            engine.run(run.run_id, WORKSPACE)
        run = engine.storage.get_run(run.run_id, WORKSPACE)
        assert run.status == RunStatus.INCOMPLETE.value

        run = engine.storage.set_run_status(run.run_id, WORKSPACE, RunStatus.CANCELLED)
        assert run.status == RunStatus.CANCELLED.value

    def test_resume_orphans_queued_attempts(self, engine):
        # §11B.6 sweeper semantics: an attempt row a dead runner left QUEUED
        # (hard kill between create_attempt and set RUNNING) is ORPHANED at
        # resume — never an IllegalTransition crash — and the run proceeds
        # to completion.
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        attempt = engine.storage.create_attempt(run_id=run.run_id, case_id="c1",
                                                workspace_id=WORKSPACE,
                                                repeat_index=0, attempt=0)
        # Simulate the hard kill: the run row left RUNNING, the attempt QUEUED
        # (never advanced to RUNNING).
        for status in (RunStatus.QUEUED, RunStatus.PROVISIONING, RunStatus.RUNNING):
            engine.storage.set_run_status(run.run_id, WORKSPACE, status)
        result = engine.run(run.run_id, WORKSPACE)
        assert result.status == RunStatus.COMPLETE.value
        orphan = engine.storage.get_attempt_by_id(attempt.attempt_id, WORKSPACE)
        assert orphan.status == AttemptStatus.ORPHANED.value
        metric = result.metric_results[0]
        assert metric.value == 1.0
        assert metric.sample_n == 2  # both cases scored


class TestJudgeMetrics:
    def _judge_spec(self):
        spec = make_spec()
        spec["metrics"] = [
            {
                "metric_id": "quality",
                "name": "response quality",
                "type": "judge",
                "target": {"type": "input", "on_missing": "fail"},
                "evaluator": {"type": "llm_judge", "rubric_version_id": "rv1"},
                "scoring": {"type": "binary", "range": [0, 1],
                            "condition": "raw_value >= 0.8"},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "judge_binding": {
                    "provider": "anthropic", "model": "claude-opus-5",
                    "schema_version": "1", "rubric_version": "1",
                },
                "provisional": True,
            }
        ]
        return spec

    def test_judge_metrics_scored_but_never_gate(self, engine):
        # §15A.1 (owner decision 4J): judge scores are usable-but-provisional
        # and excluded from gate enforcement until calibrated.
        project = make_evaluable_project(engine)
        run = engine.create_run(WORKSPACE, self._judge_spec(),
                                project_id=project.project_id, tier="quick",
                                entrypoint=AGENT)
        # The default factory is the NoopJudge — 0.5 scores, below the 0.8 bar.
        result = engine.run(run.run_id, WORKSPACE)
        metric = result.metric_results[0]
        assert metric.value == 0.0
        assert metric.gate_status == "NOT_APPLICABLE"
        assert result.gate_results == ()

    def test_scripted_judge_flows_through(self, engine):
        # A deterministic judge wired through judge_factory; its scores land
        # in the per-case rows and the run-level rollup.
        project = make_evaluable_project(engine)
        binding = JudgeBinding(provider="anthropic", model="claude-opus-5",
                               schema_version="1", rubric_version="1")
        engine_with_judge = Engine(engine.storage, work_root=engine.work_root,
                                   judge_factory=lambda b: ScriptedJudge(b, default_score=0.9))
        run = engine_with_judge.create_run(
            WORKSPACE, self._judge_spec(), project_id=project.project_id,
            tier="quick", entrypoint=AGENT,
        )
        result = engine_with_judge.run(run.run_id, WORKSPACE)
        rows = engine.storage.get_case_scores(run_id=run.run_id, workspace_id=WORKSPACE)
        assert rows and all(r.status == "PASS" for r in rows)
        assert result.metric_results[0].value == 1.0

    def test_judge_factory_binding_mismatch_is_rejected(self, engine):
        # §15A.5: judge scores are comparable only when bindings match.
        project = make_evaluable_project(engine)
        other_binding = JudgeBinding(provider="openai", model="gpt-5",
                                     schema_version="1", rubric_version="1")
        engine_with_judge = Engine(
            engine.storage, work_root=engine.work_root,
            judge_factory=lambda b: ScriptedJudge(other_binding),
        )
        run = engine_with_judge.create_run(
            WORKSPACE, self._judge_spec(), project_id=project.project_id,
            tier="quick", entrypoint=AGENT,
        )
        with pytest.raises(ValueError):
            engine_with_judge.run(run.run_id, WORKSPACE)


class TestRunCaseEndpoint:
    def test_run_case_on_non_queued_case_refused(self, engine):
        project = make_evaluable_project(engine)
        run = create_run(engine, project)
        engine.run_case(run.run_id, "c1", WORKSPACE)
        with pytest.raises(EngineError):
            engine.run_case(run.run_id, "c1", WORKSPACE)
