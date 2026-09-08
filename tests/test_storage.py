"""Tests for the SQLite persistence layer (src/llm_agent_eval/storage.py).

The §18 schema mirrored in SQLite: exact column names, enums, and CHECK
constraints; workspace_id scoping (decision 3) on every table; idempotency-key
guarded create_run (§31C.2); legal-transition-enforcing status updates; score
revisions stored ALONGSIDE machine scores (§13A.1.2) — the machine row is
never modified or deleted, only flagged ``overridden``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from llm_agent_eval.evaluators import CaseScore, CaseStatus, RunAggregate, AggregationState
from llm_agent_eval.events import CostBlock, TokenUsage, make_event
from llm_agent_eval.lifecycle import (
    AttemptStatus,
    IllegalTransition,
    RunCaseStatus,
    RunStatus,
    SmokeState,
)
from llm_agent_eval.spec import Metric, validate_spec
from llm_agent_eval.storage import Storage, case_key

SPEC = {
    "spec_version": "1",
    "name": "refund-suite",
    "dataset_version": "v1",
    "cases": [
        {"case_id": "c1", "name": "one", "input": {"order_id": "123"}},
        {"case_id": "c2", "name": "two", "input": {"order_id": "456"}},
    ],
    "metrics": [
        {
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
                        "condition": "raw_value <= 50"},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0},
            "weight": 1.0,
            "provisional": False,
        }
    ],
}


@pytest.fixture()
def storage(tmp_path):
    db = Storage(tmp_path / "eval.db")
    db.create_schema()
    yield db
    db.close()


def _spec() -> object:
    return validate_spec(SPEC)


def _metric(spec) -> Metric:
    return spec.metrics[0]


def _score(metric_id="refund_amount", case_id="c1", *, status=CaseStatus.PASS, attempt=0,
           on_retry_override=False):
    return CaseScore(
        metric_id=metric_id,
        case_id=case_id,
        score=1.0 if status is CaseStatus.PASS else 0.0,
        passed=status is CaseStatus.PASS,
        evidence_event_ids=("ev1",),
        status=status,
        message="test",
        attempt=attempt,
        on_retry_override=on_retry_override,
    )


def _make_run(storage, *, workspace_id="ws1", idempotency_key=None):
    spec = _spec()
    return storage.create_run(
        workspace_id=workspace_id,
        spec_json=spec.model_dump_json(),
        agent_version={"source_digest": "abc", "entrypoint": ["python", "-c", "pass"],
                       "cwd": None, "timeout_seconds": 120.0},
        tier="quick",
        repeat_config={"repeats": 1, "retry": {"max": 0, "authoritative": "first_attempt"},
                       "flakiness": {"classify": True}},
        world_config={},
        case_count=len(spec.cases),
        concurrency=1,
        budget_usd_micros=0,
        idempotency_key=idempotency_key,
    )


class TestSchema:
    def test_create_schema_is_idempotent(self, storage):
        storage.create_schema()  # second call must be a no-op, not an error

    def test_trace_events_has_no_fk_to_runs(self, storage):
        # Decision 4F: trace_events deliberately has no FK to runs — a
        # re-scored trace outlives the run that produced it.
        fks = storage._conn.execute(
            "PRAGMA foreign_key_list(trace_events)"
        ).fetchall()
        assert fks == []

    def test_runs_check_constraint_on_status(self, storage):
        with pytest.raises(sqlite3.IntegrityError):
            storage._conn.execute(
                "INSERT INTO runs (run_id, workspace_id, spec_json, agent_version,"
                " status, tier, repeat_config, world_config, case_count, concurrency,"
                " budget_usd_micros, progress, created_at)"
                " VALUES ('r', 'ws', '{}', '{}', 'green', 'quick', '{}', '{}', 1, 1, 0, '{}', 't')"
            )


class TestProjects:
    def test_create_and_get(self, storage):
        project = storage.create_project(workspace_id="ws1", name="refund-agent")
        assert project.smoke_state == SmokeState.INGESTED.value
        got = storage.get_project(project.project_id, "ws1")
        assert got is not None and got.name == "refund-agent"
        assert storage.get_project(project.project_id, "ws2") is None  # decision 3

    def test_smoke_state_transitions_enforced(self, storage):
        project = storage.create_project(workspace_id="ws1", name="refund-agent")
        with pytest.raises(IllegalTransition):
            storage.set_project_smoke_state(
                project.project_id, "ws1", SmokeState.RUNTIME_PREPARED
            )
        # The illegal jump was never persisted.
        assert storage.get_project(project.project_id, "ws1").smoke_state == "INGESTED"
        storage.set_project_smoke_state(project.project_id, "ws1", SmokeState.ANALYZED)
        storage.set_project_smoke_state(
            project.project_id, "ws1", SmokeState.ENTRYPOINT_MISSING,
            failure_detail="no entrypoint",
        )
        assert (
            storage.get_project(project.project_id, "ws1").smoke_failure_detail
            == "no entrypoint"
        )
        # Recovery: re-prepare from a failure state.
        storage.set_project_smoke_state(project.project_id, "ws1", SmokeState.RUNTIME_PREPARED)

    def test_smoke_attempt_history(self, storage):
        project = storage.create_project(workspace_id="ws1", name="refund-agent")
        first = storage.append_smoke_attempt(
            workspace_id="ws1", project_id=project.project_id,
            entrypoint=("python", "main.py"), state=SmokeState.NO_TRACE,
            exit_code=0, failure_detail="no trace", trace_path="/tmp/t.jsonl",
            reproduce_locally="python main.py",
        )
        second = storage.append_smoke_attempt(
            workspace_id="ws1", project_id=project.project_id,
            entrypoint=("python", "main.py"), state=SmokeState.SMOKE_PASSED,
        )
        history = storage.list_smoke_attempts(project.project_id, "ws1")
        assert [a.state for a in history] == [SmokeState.SMOKE_PASSED.value, SmokeState.NO_TRACE.value]
        assert first.reproduce_locally == "python main.py"
        assert first.trace_path == "/tmp/t.jsonl"
        assert second.trace_path is None


class TestRuns:
    def test_create_run_idempotency_key(self, storage):
        run_a = _make_run(storage, idempotency_key="key-1")
        run_b = _make_run(storage, idempotency_key="key-1")
        assert run_a.run_id == run_b.run_id
        run_c = _make_run(storage, idempotency_key="key-2")
        assert run_c.run_id != run_a.run_id
        # The key is unique per workspace (§31C.2).
        run_d = _make_run(storage, workspace_id="ws2", idempotency_key="key-1")
        assert run_d.run_id != run_a.run_id

    def test_create_run_without_key_is_never_deduplicated(self, storage):
        run_a = _make_run(storage)
        run_b = _make_run(storage)
        assert run_a.run_id != run_b.run_id

    def test_run_record_carries_spec_and_execution_config(self, storage):
        run = _make_run(storage)
        assert run.status == RunStatus.DRAFT.value
        assert run.spec.name == "refund-suite"
        assert run.entrypoint == ("python", "-c", "pass")
        assert run.repeats == 1 and run.retry_max == 0
        assert run.timeout_seconds == 120.0

    def test_run_status_transitions_and_stamps(self, storage):
        run = _make_run(storage)
        with pytest.raises(IllegalTransition):
            storage.set_run_status(run.run_id, "ws1", RunStatus.RUNNING)
        storage.set_run_status(run.run_id, "ws1", RunStatus.QUEUED)
        storage.set_run_status(run.run_id, "ws1", RunStatus.PROVISIONING)
        run = storage.set_run_status(run.run_id, "ws1", RunStatus.RUNNING)
        assert run.run_started_at is not None
        assert run.run_completed_at is None
        run = storage.set_run_status(run.run_id, "ws1", RunStatus.AGGREGATING)
        run = storage.set_run_status(run.run_id, "ws1", RunStatus.COMPLETE)
        assert run.run_completed_at is not None

    def test_run_scope_is_workspace_local(self, storage):
        run = _make_run(storage)
        assert storage.get_run(run.run_id, "ws1") is not None
        assert storage.get_run(run.run_id, "ws2") is None
        with pytest.raises(KeyError):
            storage.set_run_status(run.run_id, "ws2", RunStatus.QUEUED)

    def test_cases_insert_and_key(self, storage):
        run = _make_run(storage)
        spec = _spec()
        storage.insert_cases(run.run_id, "ws1", spec.cases, repeat_count=3)
        c1 = storage.get_case(run.run_id, "c1", "ws1")
        assert c1 is not None
        assert c1.status == RunCaseStatus.QUEUED.value
        assert c1.repeat_count == 3
        assert c1.case_key == case_key({"order_id": "123"})
        assert [c.case_id for c in storage.list_cases(run.run_id, "ws1")] == ["c1", "c2"]
        # Idempotent by PK: re-inserting does not duplicate.
        storage.insert_cases(run.run_id, "ws1", spec.cases, repeat_count=3)
        assert len(storage.list_cases(run.run_id, "ws1")) == 2

    def test_case_key_normalizes_key_order(self):
        assert case_key({"a": 1, "b": 2}) == case_key({"b": 2, "a": 1})
        assert case_key({"a": 1}) != case_key({"a": 2})

    def test_case_status_transitions(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=1)
        case = storage.set_case_status(run.run_id, "c1", "ws1", RunCaseStatus.RUNNING)
        assert case.started_at is not None
        case = storage.set_case_status(run.run_id, "c1", "ws1", RunCaseStatus.FAILED)
        assert case.completed_at is not None
        storage.set_case_classification(run.run_id, "c1", "ws1", "FAILING", error_category="agent")
        case = storage.get_case(run.run_id, "c1", "ws1")
        assert case.classification == "FAILING"
        assert case.error_category == "agent"

    def test_case_status_illegal_jump_not_persisted(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=1)
        with pytest.raises(IllegalTransition):
            storage.set_case_status(run.run_id, "c1", "ws1", RunCaseStatus.COMPLETED)
        with pytest.raises(IllegalTransition):
            storage.set_case_status(run.run_id, "c1", "ws1", RunCaseStatus.FAILED)
        assert storage.get_case(run.run_id, "c1", "ws1").status == "queued"


class TestAttempts:
    def test_create_and_status_flow(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=2)
        attempt = storage.create_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            repeat_index=1, attempt=0,
        )
        assert attempt.is_first_attempt is True
        assert attempt.status == AttemptStatus.QUEUED.value
        attempt = storage.set_attempt_status(
            attempt.attempt_id, "ws1", AttemptStatus.RUNNING
        )
        assert attempt.started_at is not None
        attempt = storage.set_attempt_status(
            attempt.attempt_id, "ws1", AttemptStatus.COMPLETED,
            exit_code=0, event_count=3,
        )
        assert attempt.finished_at is not None
        assert attempt.exit_code == 0 and attempt.event_count == 3

    def test_attempt_unique_per_repeat_attempt(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=1)
        a0 = storage.create_attempt(run_id=run.run_id, case_id="c1", workspace_id="ws1",
                                    repeat_index=0, attempt=0)
        retry = storage.create_attempt(run_id=run.run_id, case_id="c1", workspace_id="ws1",
                                       repeat_index=0, attempt=1)
        assert retry.is_first_attempt is False
        assert storage.get_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            repeat_index=0, attempt=0,
        ).attempt_id == a0.attempt_id

    def test_attempt_illegal_transition_rejected(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=1)
        attempt = storage.create_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            repeat_index=0, attempt=0,
        )
        with pytest.raises(IllegalTransition):
            storage.set_attempt_status(attempt.attempt_id, "ws1", AttemptStatus.COMPLETED)
        assert storage.get_attempt_by_id(attempt.attempt_id, "ws1").status == "queued"

    def test_attempt_values_kept_when_not_given(self, storage):
        run = _make_run(storage)
        storage.insert_cases(run.run_id, "ws1", _spec().cases, repeat_count=1)
        attempt = storage.create_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            repeat_index=0, attempt=0,
        )
        storage.set_attempt_status(attempt.attempt_id, "ws1", AttemptStatus.RUNNING)
        storage.set_attempt_status(attempt.attempt_id, "ws1", AttemptStatus.COMPLETED,
                                   exit_code=0, event_count=3, trace_truncated=True)
        again = storage.set_attempt_status(attempt.attempt_id, "ws1", AttemptStatus.COMPLETED)
        assert again.exit_code == 0 and again.event_count == 3
        assert again.trace_truncated is True


class TestTraceEvents:
    def _events(self, run, workspace_id="ws1", count=2):
        return [
            make_event(
                event_type="tool_call" if i == 0 else "tool_result",
                run_id=run.run_id,
                case_id="c1",
                workspace_id=workspace_id,
                attempt_id="a1",
                repeat_index=0,
                attempt=0,
                event_id=f"ev{i}",
                sequence=i,
                timestamp="2026-01-01T00:00:00Z",
                tool="refund_order",
                cost=CostBlock(
                    tokens=TokenUsage(input=10, output=20),
                    cost_usd=0.0012,
                    price_version="2026-08",
                ),
                payload={"output": {"amount": 49.99}},
            )
            for i in range(count)
        ]

    def test_insert_and_roundtrip(self, storage):
        run = _make_run(storage)
        events = self._events(run)
        storage.insert_trace_events(events)
        got = storage.get_trace_events(
            run_id=run.run_id, workspace_id="ws1", case_id="c1", attempt_id="a1"
        )
        assert len(got) == 2
        assert [e.event_id for e in got] == ["ev0", "ev1"]  # (timestamp, sequence) order
        assert got[0].tool == "refund_order"
        assert got[0].cost.price_version == "2026-08"
        assert got[0].cost.cost_usd == 0.0012

    def test_insert_ignores_duplicate_event_ids(self, storage):
        run = _make_run(storage)
        events = self._events(run)
        storage.insert_trace_events(events)
        storage.insert_trace_events(events)  # (run_id, event_id) PK: OR IGNORE
        got = storage.get_trace_events(run_id=run.run_id, workspace_id="ws1")
        assert len(got) == 2

    def test_workspace_isolation(self, storage):
        run = _make_run(storage)
        storage.insert_trace_events(self._events(run))
        assert storage.get_trace_events(
            run_id=run.run_id, workspace_id="ws2"
        ) == []
        assert storage.get_trace_events(
            run_id=run.run_id, workspace_id="ws1"
        ) != []

    def test_storage_redaction_updates_trace_state(self, storage):
        run = _make_run(storage)
        event = self._events(run, count=1)[0].model_copy(update={
            "payload": {"api_key": "sk-test-do-not-persist-0001"},
        })
        storage.insert_trace_events([event])
        stored = storage.get_trace_events(run_id=run.run_id, workspace_id="ws1")[0]
        assert "sk-test-do-not-persist-0001" not in str(stored.payload)
        assert stored.redaction_state.status == "redacted"
        assert stored.redaction_state.rules

    def test_storage_marks_truncated_trace_payloads(self, storage):
        run = _make_run(storage)
        event = self._events(run, count=1)[0].model_copy(update={"payload": {"log": "x" * 20_000}})
        storage.insert_trace_events([event])
        stored = storage.get_trace_events(run_id=run.run_id, workspace_id="ws1")[0]
        assert stored.redaction_state.status == "truncated"
        assert "storage:truncated" in stored.redaction_state.rules


class TestCostSummaries:
    """§12B.2: per-price_version totals written at finalize; the engine's
    historical-cost surface. PK is (run_id, price_version) — re-writing a
    run's finalize totals must UPSERT, never duplicate."""

    def test_add_get_roundtrip(self, storage):
        run = _make_run(storage)
        storage.add_cost_summaries(run.run_id, "ws1", [
            {"price_version": "2026-08", "input_tokens": 60, "output_tokens": 120,
             "usd_micros": 7200},
            {"price_version": "2025-11", "input_tokens": 10, "output_tokens": 20,
             "usd_micros": 1000},
        ])
        got = storage.get_cost_summaries(run.run_id, "ws1")
        assert {g["price_version"] for g in got} == {"2026-08", "2025-11"}
        by_pv = {g["price_version"]: g for g in got}
        assert by_pv["2026-08"]["input_tokens"] == 60
        assert by_pv["2026-08"]["output_tokens"] == 120
        assert by_pv["2026-08"]["usd_micros"] == 7200

    def test_upsert_replaces_same_price_version(self, storage):
        run = _make_run(storage)
        storage.add_cost_summaries(run.run_id, "ws1", [
            {"price_version": "2026-08", "input_tokens": 60, "output_tokens": 120,
             "usd_micros": 7200},
        ])
        # A re-finalize with different totals must replace, not accumulate.
        storage.add_cost_summaries(run.run_id, "ws1", [
            {"price_version": "2026-08", "input_tokens": 70, "output_tokens": 140,
             "usd_micros": 8400},
        ])
        got = storage.get_cost_summaries(run.run_id, "ws1")
        assert len(got) == 1
        assert got[0]["usd_micros"] == 8400

    def test_workspace_isolation(self, storage):
        run = _make_run(storage)
        storage.add_cost_summaries(run.run_id, "ws1", [
            {"price_version": "2026-08", "input_tokens": 60, "output_tokens": 120,
             "usd_micros": 7200},
        ])
        assert storage.get_cost_summaries(run.run_id, "ws2") == []

    def test_iter_cost_json_only_returns_cost_bearing_events(self, storage):
        run = _make_run(storage)
        storage.insert_trace_events(self._cost_events(run))
        # one event without cost must not appear
        storage.insert_trace_events([
            make_event(
                event_type="delegation", run_id=run.run_id, case_id="c1",
                workspace_id="ws1", attempt_id="a1", repeat_index=0, attempt=0,
                event_id="no-cost", sequence=9, timestamp="2026-01-01T00:00:01Z",
            )
        ])
        raw = list(storage.iter_cost_json(run.run_id, "ws1"))
        assert len(raw) == 2
        assert all(r["price_version"] == "2026-08" for r in raw)
        assert sum(r["cost_usd"] for r in raw) == 0.0024

    def _cost_events(self, run, count=2):
        return [
            make_event(
                event_type="llm_call" if i == 0 else "llm_response",
                run_id=run.run_id, case_id="c1", workspace_id="ws1",
                attempt_id="a1", repeat_index=0, attempt=0,
                event_id=f"cost-ev{i}", sequence=i,
                timestamp="2026-01-01T00:00:00Z",
                cost=CostBlock(
                    tokens=TokenUsage(input=10, output=20),
                    cost_usd=0.0012,
                    price_version="2026-08",
                ),
            )
            for i in range(count)
        ]


class TestMetricResults:
    def test_case_metric_results_upsert_and_flags(self, storage):
        run = _make_run(storage)
        spec = _spec()
        metric = spec.metrics[0]
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score()], metric_by_id={metric.metric_id: metric},
        )
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        assert len(rows) == 1
        assert rows[0].status == "PASS"
        assert rows[0].is_authoritative is True
        assert rows[0].on_retry_override is False
        # Re-score in place: last attempt wins under the same revision (§18.6).
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score(status=CaseStatus.FAIL)],
            metric_by_id={metric.metric_id: metric},
        )
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        assert len(rows) == 1
        assert rows[0].status == "FAIL"

    def test_retry_row_flagged_on_retry_override(self, storage):
        run = _make_run(storage)
        spec = _spec()
        metric = spec.metrics[0]
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score(attempt=1, on_retry_override=True, status=CaseStatus.PASS)],
            metric_by_id={metric.metric_id: metric},
        )
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        assert rows[0].is_authoritative is False
        assert rows[0].on_retry_override is True

    def test_judge_binding_persisted(self, storage):
        spec_dict = json.loads(_spec().model_dump_json())
        spec_dict["metrics"][0].update({
            "type": "judge",
            "evaluator": {"type": "llm_judge", "rubric_version_id": "rv1"},
            "scoring": {"type": "binary", "range": [0, 1],
                        "condition": "raw_value >= 0.5"},
            "judge_binding": {"provider": "anthropic", "model": "claude-opus-5",
                              "schema_version": "1", "rubric_version": "1"},
            "provisional": True,
        })
        spec = validate_spec(spec_dict)
        run = storage.create_run(
            workspace_id="ws1", spec_json=spec.model_dump_json(),
            agent_version={"source_digest": "x", "entrypoint": ["python"], "cwd": None,
                           "timeout_seconds": 120.0},
            tier="quick",
            repeat_config={"repeats": 1, "retry": {"max": 0, "authoritative": "first_attempt"},
                           "flakiness": {"classify": True}},
            world_config={}, case_count=2, concurrency=1, budget_usd_micros=0,
        )
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score(metric_id="refund_amount")],
            metric_by_id={spec.metrics[0].metric_id: spec.metrics[0]},
        )
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        assert rows[0].judge_binding == {"provider": "anthropic", "model": "claude-opus-5",
                                         "schema_version": "1", "rubric_version": "1"}

    def test_run_metric_result_upsert(self, storage):
        run = _make_run(storage)
        spec = _spec()
        metric = spec.metrics[0]
        aggregate = RunAggregate(
            metric_id=metric.metric_id, value=1.0, pass_rate=1.0, mean=1.0,
            n_cases=1, n_denominator=1, error_n=0, skipped_n=0,
            aggregation_state=AggregationState.IN_PROGRESS,
        )
        storage.upsert_run_metric_result(
            run_id=run.run_id, workspace_id="ws1", metric=metric,
            aggregate=aggregate, no_ci=True,
        )
        rows = storage.get_run_metric_results(run.run_id, "ws1")
        assert len(rows) == 1
        row = rows[0]
        assert row.value == 1.0
        assert row.aggregation_state == "IN_PROGRESS"
        assert row.method == "pass_rate" and row.on_error == "fail"
        assert row.no_ci is True
        assert row.gate_status is None
        # Upsert in place, not a duplicate row.
        aggregate = RunAggregate(
            metric_id=metric.metric_id, value=0.5, pass_rate=0.5, mean=0.5,
            n_cases=1, n_denominator=2, error_n=1, skipped_n=0,
            aggregation_state=AggregationState.COMPLETE,
        )
        storage.upsert_run_metric_result(
            run_id=run.run_id, workspace_id="ws1", metric=metric,
            aggregate=aggregate, gate_status="FAIL",
        )
        rows = storage.get_run_metric_results(run.run_id, "ws1")
        assert len(rows) == 1
        assert rows[0].aggregation_state == "COMPLETE"
        assert rows[0].error_n == 1
        assert rows[0].gate_status == "FAIL"

    def test_run_metric_result_rejects_unknown_gate_status(self, storage):
        run = _make_run(storage)
        spec = _spec()
        with pytest.raises(ValueError):
            storage.upsert_run_metric_result(
                run_id=run.run_id, workspace_id="ws1", metric=spec.metrics[0],
                aggregate=RunAggregate(
                    metric_id=spec.metrics[0].metric_id, value=1.0, pass_rate=1.0,
                    mean=1.0, n_cases=1, n_denominator=1, error_n=0, skipped_n=0,
                    aggregation_state=AggregationState.COMPLETE,
                ),
                gate_status="GREEN",
            )


class TestScoreRevisions:
    def test_override_stored_alongside_machine_row(self, storage):
        # §13A.1.2: the machine row is never modified or deleted — the
        # override value sits beside it, and the machine row is flagged.
        run = _make_run(storage)
        spec = _spec()
        metric = spec.metrics[0]
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score(status=CaseStatus.FAIL)],
            metric_by_id={metric.metric_id: metric},
        )
        revision = storage.append_score_revision(
            run_id=run.run_id, case_id="c1", metric_id=metric.metric_id,
            workspace_id="ws1", score_revision=2,
            machine_score_snapshot={"status": "FAIL", "score": 0.0},
            override_value={"status": "PASS", "score": 1.0},
            reason="disputed — the agent refunded in a follow-up call",
        )
        assert revision.score_revision == 2
        assert revision.status == "open"
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        # The machine row is untouched: still FAIL, still score 0.0, but
        # flagged overridden for ordering.
        assert rows[0].status == "FAIL"
        assert rows[0].score == 0.0
        assert rows[0].overridden is True
        revisions = storage.get_score_revisions(run_id=run.run_id, workspace_id="ws1")
        assert len(revisions) == 1
        assert revisions[0].override_value == {"status": "PASS", "score": 1.0}
        assert revisions[0].machine_score_snapshot == {"status": "FAIL", "score": 0.0}

    def test_revision_without_override_does_not_flag(self, storage):
        run = _make_run(storage)
        spec = _spec()
        metric = spec.metrics[0]
        storage.upsert_case_metric_results(
            run_id=run.run_id, case_id="c1", workspace_id="ws1",
            scores=[_score()], metric_by_id={metric.metric_id: metric},
        )
        storage.append_score_revision(
            run_id=run.run_id, case_id="c1", metric_id=metric.metric_id,
            workspace_id="ws1", score_revision=2,
            machine_score_snapshot={"status": "PASS", "score": 1.0},
        )
        rows = storage.get_case_scores(run_id=run.run_id, workspace_id="ws1")
        assert rows[0].overridden is False


def test_shared_connection_is_thread_safe(storage):
    """One sqlite3 connection serves the API's request threads AND the
    background run threads (§17). Locking only ``execute()`` leaves the lazy
    fetch unguarded — observed under contention: torn JSON values (a progress
    column reading back as the empty string), SELECTs returning no rows for an
    existing record (the intermittent GET /runs/{id} 404), and plain-tuple
    rows (the list_cases IndexError). Regression: the whole execute→fetch span
    must be serialized."""
    run = _make_run(storage)
    spec = _spec()
    storage.insert_cases(run.run_id, "ws1", spec.cases, repeat_count=1)
    run_id = run.run_id
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                rec = storage.get_run(run_id, "ws1")
                assert rec is not None and rec.run_id == run_id
                assert rec.spec is not None  # a torn progress JSON raises here
                cases = storage.list_cases(run_id, "ws1")
                assert len(cases) == 2
                assert all(c.case_id in ("c1", "c2") for c in cases)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            stop.set()

    def writer() -> None:
        try:
            while not stop.is_set():
                storage.set_run_progress(run_id, "ws1", {"case_id": "c1"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            stop.set()

    threads = [threading.Thread(target=reader) for _ in range(4)] + [
        threading.Thread(target=writer) for _ in range(2)
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not errors:
        time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads), "a worker thread hung"
    assert not errors, [f"{type(e).__name__}: {e}" for e in errors]
