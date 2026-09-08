"""Disposable service driver: all scenario writes use public repositories/APIs."""

import io
import zipfile
from dataclasses import asdict

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
        if scenario == "two_workspace_projects":
            first = self.store.create_project(workspace_id="workspace_a", name="Refund agent")
            second = self.store.create_project(workspace_id="workspace_b", name="Inventory agent")
            return {"project_id": first.project_id, "other_project_id": second.project_id}
        first = self.store.create_project(workspace_id="workspace_a", name="Refund agent")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            if scenario == "valid_zip":
                zipped.writestr("agent.py", "def answer(question):\n    return 'ok'\n")
                zipped.writestr(".env", "PROVIDER_TOKEN=sk-test-do-not-persist-0001")
                zipped.writestr("settings.py", "API_KEY = 'sk-test-do-not-persist-0001'")
            elif scenario == "reference_broken_zip":
                zipped.writestr("broken.py", "def unfinished(:\n")
            elif scenario == "zip_traversal":
                zipped.writestr("../escape.py", "print('never execute')")
            elif scenario == "zip_case_collision":
                zipped.writestr("Agent.py", "x = 1")
                zipped.writestr("agent.py", "x = 2")
            elif scenario == "zip_expansion_limit":
                zipped.writestr("large.txt", "x" * 4096, compress_type=zipfile.ZIP_DEFLATED)
            elif scenario == "nested_archive":
                inner = io.BytesIO()
                with zipfile.ZipFile(inner, "w") as inner_zip:
                    inner_zip.writestr("inner.py", "x = 1")
                zipped.writestr("nested.zip", inner.getvalue())
            elif scenario == "missing_git_ref":
                return {"project_id": first.project_id, "request": {
                    "kind": "git", "repo_url": "https://example.invalid/agent.git", "ref": "missing-ref",
                }}
            elif scenario == "unapproved_git_host":
                return {"project_id": first.project_id, "request": {
                    "kind": "git", "repo_url": "https://unapproved.example.com/agent.git", "ref": "main",
                }}
            else:
                raise AssertionError(f"unknown scenario: {scenario}")
        upload = self.clients["editor"].post("/api/uploads", content=archive.getvalue(), headers={"content-type": "application/zip"})
        assert upload.status_code == 201, upload.text
        return {"project_id": first.project_id, "request": {"kind": "zip", "upload_id": upload.json()["upload_id"]}}

    def post(self, path, body, *, key=None, actor="editor"):
        headers = {"Idempotency-Key": key} if key else {}
        response = self.clients[actor].post(path, json=body, headers=headers)
        return {"http_status": response.status_code, "body": response.json()}

    def get(self, path, *, actor="editor"):
        response = self.clients[actor].get(path)
        return {"http_status": response.status_code, "body": response.json()}

    def artifact_bytes(self, artifact_id, *, actor="editor"):
        response = self.clients[actor].get(f"/api/artifacts/{artifact_id}")
        assert response.status_code == 200, response.text
        return response.content

    def drain(self, job_id, *, actor="editor"):
        return asdict(self.clients[actor].app.state.import_service.drain(self.clients[actor].app.state.workflow_actor, job_id))

    def close(self):
        for client in self.clients.values():
            client.close()
        self.store.close()

    def restart(self):
        self.close()
        self.__init__(self.root)
