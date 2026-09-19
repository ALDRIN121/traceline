from __future__ import annotations

from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


def _fixture(tmp_path):
    storage = Storage(tmp_path / "schedule-api.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="scheduled")
    versions = VersionStore(storage)
    evaluation = versions.create(
        "evaluation", project.project_id, {"spec": {"name": "e", "cases": []}}, 0, actor,
    )
    dataset = versions.create("dataset", project.project_id, {"cases": []}, 0, actor)
    plans = RunPlanService(storage)
    plan = plans.plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick"})
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    client = TestClient(create_app(
        storage=storage, workspace_id="ws", artifact_root=tmp_path / "artifacts",
        gateway=MockGateway({}),
    ))
    other_client = TestClient(create_app(
        storage=storage, workspace_id="other", artifact_root=tmp_path / "other-artifacts",
        gateway=MockGateway({}),
    ))
    return storage, client, other_client, plan, authorization


def test_schedule_api_lists_details_and_controls_lifecycle(tmp_path):
    storage, client, other_client, plan, authorization = _fixture(tmp_path)
    try:
        created = client.post("/api/schedules", json={
            "plan_id": plan.plan_id,
            "plan_hash": plan.content_digest,
            "authorization_id": authorization.authorization_id,
            "timezone": "Asia/Kolkata",
            "local_time": "09:00",
            "daily_request_limit": 2,
        })
        assert created.status_code == 201
        schedule = created.json()["schedule"]
        schedule_id = schedule["schedule_id"]
        assert schedule["state"] == "active"

        listed = client.get("/api/schedules")
        assert listed.status_code == 200
        assert [item["schedule_id"] for item in listed.json()["schedules"]] == [schedule_id]

        detail = client.get(f"/api/schedules/{schedule_id}")
        assert detail.status_code == 200
        assert detail.json()["schedule"]["schedule_id"] == schedule_id
        assert detail.json()["slots"] == []

        paused = client.post(f"/api/schedules/{schedule_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["state"] == "paused"
        resumed = client.post(f"/api/schedules/{schedule_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "active"
        assert other_client.get(f"/api/schedules/{schedule_id}").status_code == 404
        assert other_client.post(f"/api/schedules/{schedule_id}/pause").status_code == 404
    finally:
        client.close()
        other_client.close()
        storage.close()
