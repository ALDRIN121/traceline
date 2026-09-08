"""Disposable service driver: all scenario writes use public repositories/APIs."""

from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.storage import Storage


class WorkflowDriver:
    def __init__(self, root):
        self.root = root
        self.store = Storage(root / "workflow.db")
        self.store.create_schema()
        self.clients = {
            "editor": TestClient(create_app(storage=self.store, workspace_id="workspace_a", artifact_root=root / "artifacts")),
            "other_editor": TestClient(create_app(storage=self.store, workspace_id="workspace_b", artifact_root=root / "artifacts")),
        }

    def seed(self, scenario):
        assert scenario == "two_workspace_projects"
        first = self.store.create_project(workspace_id="workspace_a", name="Refund agent")
        second = self.store.create_project(workspace_id="workspace_b", name="Inventory agent")
        return {"project_id": first.project_id, "other_project_id": second.project_id}

    def post(self, path, body, *, key=None, actor="editor"):
        headers = {"Idempotency-Key": key} if key else {}
        response = self.clients[actor].post(path, json=body, headers=headers)
        return {"http_status": response.status_code, "body": response.json()}

    def get(self, path, *, actor="editor"):
        response = self.clients[actor].get(path)
        return {"http_status": response.status_code, "body": response.json()}

    def close(self):
        for client in self.clients.values():
            client.close()
        self.store.close()

    def restart(self):
        self.close()
        self.__init__(self.root)
