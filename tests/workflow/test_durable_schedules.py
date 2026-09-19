from __future__ import annotations

from datetime import date, time
import json

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.schedules import DurableScheduleService, due_daily_slots
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker
from llm_agent_eval.contracts import WorkflowError


def test_schedule_slots_are_persistent_and_idempotent(tmp_path):
    storage = Storage(tmp_path / "schedule.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="scheduled")
    versions = VersionStore(storage)
    evaluation = versions.create("evaluation", project.project_id, {"spec": {"name": "e", "cases": []}}, 0, actor)
    dataset = versions.create("dataset", project.project_id, {"cases": []}, 0, actor)
    plans = RunPlanService(storage)
    plan = plans.plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick"})
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    worker = WorkflowWorker(storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32)
    service = DurableScheduleService(storage)
    schedule = service.create(
        actor,
        plan_id=plan.plan_id,
        plan_hash=plan.content_digest,
        authorization_id=authorization.authorization_id,
        timezone_name="Asia/Kolkata",
        local_time="09:00",
        daily_request_limit=2,
    )

    first = worker.sweep_schedules(actor, schedule.schedule_id, date(2026, 3, 7), date(2026, 3, 8), owner="worker-a")
    second = worker.sweep_schedules(actor, schedule.schedule_id, date(2026, 3, 7), date(2026, 3, 8), owner="worker-b")

    assert len(first) == 2
    assert second == []
    slots = storage.list_schedule_slots(schedule.schedule_id, "ws")
    assert [slot.state for slot in slots] == ["queued", "queued"]
    assert len({slot.job_id for slot in slots}) == 2


def test_daily_schedule_can_skip_nonexistent_dst_time():
    slots = due_daily_slots(
        date(2026, 3, 8), date(2026, 3, 8),
        time(2, 30), "America/New_York", dst_policy="skip",
    )
    assert slots == []


def test_schedule_lifecycle_can_pause_resume_and_stays_durable(tmp_path):
    storage = Storage(tmp_path / "schedule-lifecycle.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="scheduled")
    versions = VersionStore(storage)
    evaluation = versions.create("evaluation", project.project_id, {"spec": {"name": "e", "cases": []}}, 0, actor)
    dataset = versions.create("dataset", project.project_id, {"cases": []}, 0, actor)
    plans = RunPlanService(storage)
    plan = plans.plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick"})
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    service = DurableScheduleService(storage)
    schedule = service.create(
        actor, plan_id=plan.plan_id, plan_hash=plan.content_digest,
        authorization_id=authorization.authorization_id,
        timezone_name="Asia/Kolkata", local_time="09:00",
    )

    paused = service.set_state(actor, schedule.schedule_id, "paused")
    assert paused.state == "paused"
    assert storage.get_schedule(schedule.schedule_id, actor.workspace_id).state == "paused"
    resumed = service.set_state(actor, schedule.schedule_id, "active")
    assert resumed.state == "active"
    assert storage.get_schedule(schedule.schedule_id, actor.workspace_id).state == "active"
    storage.close()


def test_schedule_budget_reconciles_completed_slot_from_authoritative_cost_ledger(tmp_path):
    storage = Storage(tmp_path / "schedule-ledger.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="scheduled")
    versions = VersionStore(storage)
    evaluation = versions.create("evaluation", project.project_id, {"spec": {"name": "e", "cases": []}}, 0, actor)
    dataset = versions.create("dataset", project.project_id, {"cases": []}, 0, actor)
    plans = RunPlanService(storage)
    plan = plans.plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick", "budget_usd_micros": 100})
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    worker = WorkflowWorker(storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32)
    schedule = DurableScheduleService(storage).create(
        actor, plan_id=plan.plan_id, plan_hash=plan.content_digest,
        authorization_id=authorization.authorization_id,
        timezone_name="Asia/Kolkata", local_time="09:00",
        daily_budget_usd_micros=150,
    )
    jobs = worker.sweep_schedules(
        actor, schedule.schedule_id, date(2026, 3, 7), date(2026, 3, 7), owner="worker-a",
    )
    slot = storage.list_schedule_slots(schedule.schedule_id, "ws")[0]
    run = storage.create_run(
        workspace_id="ws", project_id=project.project_id,
        spec_json=json.dumps({"spec_version": "1", "name": "ledger",
                              "dataset_version": dataset.version_id,
                              "cases": [], "metrics": []}),
        agent_version={}, tier="quick", repeat_config={"repeats": 1}, world_config={},
        case_count=0, concurrency=1, budget_usd_micros=100,
        idempotency_key="ledger-run",
    )
    storage.add_cost_summaries("%s" % run.run_id, "ws", [{
        "price_version": "fixture-v1", "input_tokens": 1,
        "output_tokens": 1, "usd_micros": 17,
    }])
    with storage.workspace_transaction("ws") as conn:
        conn.execute(
            "UPDATE jobs SET status='completed', result_json=?, result_redaction_json=? WHERE workspace_id=? AND job_id=?",
            (json.dumps({"run_id": run.run_id}), json.dumps({"version": "v1", "detector_flags": [], "truncated": False}),
             "ws", jobs[0].job_id),
        )

    usage = storage.get_schedule_daily_cost_usage(
        schedule.schedule_id, "ws", reservation_usd_micros=100,
    )
    assert usage["2026-03-07"] == {
        "actual_usd_micros": 17, "reserved_usd_micros": 0, "unknown": False,
    }
    storage.close()


def test_schedule_rejects_unimplemented_version_refresh_policy(tmp_path):
    storage = Storage(tmp_path / "schedule-policy.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="scheduled")
    versions = VersionStore(storage)
    evaluation = versions.create("evaluation", project.project_id, {"spec": {"name": "e", "cases": []}}, 0, actor)
    dataset = versions.create("dataset", project.project_id, {"cases": []}, 0, actor)
    plans = RunPlanService(storage)
    plan = plans.plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick"})
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    try:
        with pytest.raises(WorkflowError, match="only frozen version policy"):
            DurableScheduleService(storage).create(
                actor, plan_id=plan.plan_id, plan_hash=plan.content_digest,
                authorization_id=authorization.authorization_id,
                timezone_name="Asia/Kolkata", local_time="09:00",
                version_policy="new_versions",
            )
    finally:
        storage.close()
