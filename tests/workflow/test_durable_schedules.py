from __future__ import annotations

from datetime import date, time

from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.schedules import DurableScheduleService, due_daily_slots
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


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
