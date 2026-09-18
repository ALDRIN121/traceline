from __future__ import annotations

import json

from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import Actor, create_install_auth_resolver
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.spec import validate_spec
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


def _fixture(tmp_path):
    storage = Storage(tmp_path / "ci.db")
    storage.create_schema()
    workspace = "ci-workspace"
    actor = Actor("owner", workspace, "owner")
    project = storage.create_project(workspace_id=workspace, name="ci agent")
    evaluation = VersionStore(storage).create(
        "evaluation", project.project_id,
        {"spec": {"name": "ci", "cases": [], "metrics": []}}, 0, actor,
    )
    dataset = VersionStore(storage).create(
        "dataset", project.project_id, {"cases": []}, 0, actor,
    )
    plan = RunPlanService(storage).plan_run(actor, {
        "project_id": project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {})
    authorization = RunPlanService(storage).authorize(actor, plan.plan_id, plan.content_digest)
    token_path = tmp_path / "install" / "owner.token"
    resolver = create_install_auth_resolver(token_path, workspace)
    app = create_app(
        storage=storage, workspace_id=workspace, artifact_root=tmp_path / "artifacts",
        gateway=MockGateway({}), auth_resolver=resolver, release_mode=True,
    )
    return storage, TestClient(app), token_path.read_text(encoding="utf-8").strip(), plan, authorization


def _headers(token, key):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def test_ci_submission_is_authenticated_and_idempotent(tmp_path):
    storage, client, token, plan, authorization = _fixture(tmp_path)
    try:
        payload = {
            "plan_id": plan.plan_id,
            "plan_hash": plan.content_digest,
            "authorization_id": authorization.authorization_id,
        }
        assert client.post("/api/ci/runs", json=payload).status_code == 401

        first = client.post("/api/ci/runs", json=payload, headers=_headers(token, "ci-1"))
        second = client.post("/api/ci/runs", json=payload, headers=_headers(token, "ci-1"))
        assert first.status_code == second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        assert first.json()["state"] == "queued"
    finally:
        storage.close()


def test_webhook_submission_uses_delivery_id_as_idempotency_key(tmp_path):
    storage, client, token, plan, authorization = _fixture(tmp_path)
    try:
        payload = {
            "plan_id": plan.plan_id,
            "plan_hash": plan.content_digest,
            "authorization_id": authorization.authorization_id,
        }
        assert client.post("/api/webhooks/evaluations", json=payload).status_code == 401
        headers = {"Authorization": f"Bearer {token}", "X-Webhook-Delivery": "delivery-1"}
        first = client.post("/api/webhooks/evaluations", json=payload, headers=headers)
        second = client.post("/api/webhooks/evaluations", json=payload, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
    finally:
        storage.close()


def test_ci_status_and_comparison_routes_are_authenticated(tmp_path):
    storage, client, token, _plan, _authorization = _fixture(tmp_path)
    try:
        assert client.get("/api/ci/runs/missing").status_code == 401
        assert client.post("/api/comparisons", json={}).status_code == 401

        spec = validate_spec({
            "spec_version": "1", "name": "ci", "dataset_version": "v1",
            "cases": [], "metrics": [],
        })
        run = storage.create_run(
            workspace_id="ci-workspace", spec_json=json.dumps(spec.model_dump(mode="json")),
            agent_version={"source_digest": "source"}, tier="quick",
            repeat_config={"repeats": 1, "retry_max": 0}, world_config={},
            case_count=0, concurrency=1, budget_usd_micros=0,
        )
        storage.set_run_status(run.run_id, "ci-workspace", "queued")
        storage.set_run_status(run.run_id, "ci-workspace", "provisioning")
        storage.set_run_status(run.run_id, "ci-workspace", "running")
        storage.set_run_status(run.run_id, "ci-workspace", "aggregating")
        storage.set_run_status(run.run_id, "ci-workspace", "complete")
        auth = {"Authorization": f"Bearer {token}"}
        status = client.get(f"/api/ci/runs/{run.run_id}", headers=auth)
        assert status.status_code == 200
        assert status.json()["status"] == "complete"
        assert status.json()["exit_code"] == 0

        missing = client.post("/api/comparisons", json={"baseline_run_id": "x", "candidate_run_id": "y", "metric_id": "m"}, headers=auth)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
    finally:
        storage.close()
