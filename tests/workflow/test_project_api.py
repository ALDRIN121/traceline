from __future__ import annotations

from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.storage import Storage


def _clients(tmp_path):
    storage = Storage(tmp_path / "projects.db")
    storage.create_schema()
    client = TestClient(create_app(
        storage=storage, workspace_id="workspace-a",
        artifact_root=tmp_path / "artifacts-a", gateway=MockGateway({}),
    ))
    other = TestClient(create_app(
        storage=storage, workspace_id="workspace-b",
        artifact_root=tmp_path / "artifacts-b", gateway=MockGateway({}),
    ))
    return storage, client, other


def test_project_bootstrap_lists_and_reads_only_workspace_projects(tmp_path):
    storage, client, other = _clients(tmp_path)
    try:
        created = client.post("/api/projects", json={
            "name": "Refund agent",
            "entrypoint": ["python", "agent.py"],
        })
        assert created.status_code == 201
        project = created.json()["project"]
        assert created.json()["state"] == "created"
        assert project["name"] == "Refund agent"
        assert project["entrypoint"] == ["python", "agent.py"]
        assert project["smoke_state"] == "INGESTED"

        listed = client.get("/api/projects")
        assert listed.status_code == 200
        assert [item["project_id"] for item in listed.json()["projects"]] == [project["project_id"]]

        detail = client.get(f"/api/projects/{project['project_id']}")
        assert detail.status_code == 200
        assert detail.json()["project"]["project_id"] == project["project_id"]
        assert other.get("/api/projects").json()["projects"] == []
        assert other.get(f"/api/projects/{project['project_id']}").status_code == 404
    finally:
        client.close()
        other.close()
        storage.close()


def test_project_bootstrap_rejects_blank_or_malformed_entrypoint(tmp_path):
    storage, client, _other = _clients(tmp_path)
    try:
        blank = client.post("/api/projects", json={"name": "   "})
        assert blank.status_code == 422
        malformed = client.post("/api/projects", json={"name": "agent", "entrypoint": "python agent.py"})
        assert malformed.status_code == 422
    finally:
        client.close()
        storage.close()
