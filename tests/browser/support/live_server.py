"""Disposable FastAPI server for browser acceptance coverage."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn

from llm_agent_eval.api import create_app
from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.engine import Engine
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.knowledge import KnowledgeStore
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.versions import VersionStore


WORKSPACE = "browser-workspace"
ACTOR = Actor("browser-owner", WORKSPACE, "owner")
TARGET_STUB_PORT = 8766
LIVE_EVAL_ID = "live-browser-eval"


class _TargetStubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/invoke":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        payload = json.dumps({"answer": "verified"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def _start_target_stub():
    server = ThreadingHTTPServer(("127.0.0.1", TARGET_STUB_PORT), _TargetStubHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="browser-target-stub",
        daemon=True,
    )
    thread.start()
    return server, thread


def _source_archive() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "src/support/agent.py",
            "def build_tools():\n"
            "    return {\"lookup_order_status\": object()}\n",
        )
    return archive.getvalue()


def _seed(storage: Storage, artifact_root: Path) -> dict[str, str]:
    project = storage.create_project(
        workspace_id=WORKSPACE,
        name="Seeded support agent",
        entrypoint=("python", "agent.py"),
    )
    artifact = ArtifactStore(storage, artifact_root, ACTOR).put(
        WORKSPACE, _source_archive(), "application/zip"
    )
    VersionStore(storage).create(
        "source",
        project.project_id,
        {"artifact_ids": [artifact.artifact_id], "readiness": "blocked", "manifest": {}},
        0,
        ACTOR,
    )
    KnowledgeStore(storage, artifact_root).current(project.project_id, ACTOR)

    plan_project = storage.create_project(
        workspace_id=WORKSPACE,
        name="Seeded live run agent",
        entrypoint=("python", "agent.py"),
    )
    VersionStore(storage).create(
        "source",
        plan_project.project_id,
        {"artifact_ids": [artifact.artifact_id], "readiness": "executable", "manifest": {}},
        0,
        ACTOR,
    )
    KnowledgeStore(storage, artifact_root).current(plan_project.project_id, ACTOR)

    connection = ConnectionService(storage, artifact_root / "install-secret.key")
    target = connection.create(
        ACTOR,
        plan_project.project_id,
        {
            "url": f"http://127.0.0.1:{TARGET_STUB_PORT}/invoke",
            "framework": "generic_http",
            "mode": "stateless_json",
            "auth": {"type": "none"},
        },
    )
    verified = connection.verify(
        ACTOR,
        target["target_id"],
        {"target_version_id": target["version_id"], "smoke_input": {"message": "seed verification"}},
    )

    cases = [{"case_id": "live-case", "name": "live hosted case", "input": {"message": "hello"}}]
    versions = VersionStore(storage)
    dataset = versions.create(
        "dataset", plan_project.project_id, {"cases": cases}, 0, ACTOR,
    )
    spec = {
        "spec_version": "1",
        "name": "Live hosted acceptance",
        "dataset_version": dataset.version_id,
        "cases": cases,
        "metrics": [{
            "metric_id": "answer",
            "name": "Observed answer",
            "type": "scalar",
            "target": {"type": "final_response", "selector": "$", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "verified"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0},
            "provisional": False,
        }],
    }
    dashboard = {
        "version": 3,
        "name": "Live results",
        "registry_version": 1,
        "layout": {"type": "grid", "columns": 12},
        "filters": [{"id": "run", "type": "run_selector", "default": "latest"}],
        "blocks": [{
            "component": "metric_summary",
            "span": 12,
            "bind": {"metric": "answer", "run": "$filters.run", "stat": "pass_rate"},
        }],
    }
    storage.create_custom_eval(
        workspace_id=WORKSPACE,
        name="Live hosted acceptance",
        spec=spec,
        dashboard={"definition": dashboard, "target_version_id": verified["target_version_id"]},
        dataset=cases,
        project_id=plan_project.project_id,
        entrypoint=("python", "agent.py"),
        eval_id=LIVE_EVAL_ID,
    )
    evaluation = versions.create(
        "evaluation", LIVE_EVAL_ID, {"spec": spec}, 0, ACTOR,
    )
    dashboard_version = versions.create(
        "dashboard", LIVE_EVAL_ID, {"definition": dashboard}, 0, ACTOR,
    )
    return {
        "project_id": plan_project.project_id,
        "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
        "dashboard_version_id": dashboard_version.version_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="traceline-browser-") as temporary:
        root = Path(temporary)
        storage = Storage(root / "browser.db")
        storage.create_schema()
        artifact_root = root / "artifacts"
        previous_allowlist = os.environ.get("EVAL_ENGINE_ALLOW_ENDPOINTS")
        os.environ["EVAL_ENGINE_ALLOW_ENDPOINTS"] = f"127.0.0.1:{TARGET_STUB_PORT}"
        target_stub = None
        target_stub_thread = None
        try:
            target_stub, target_stub_thread = _start_target_stub()
            _seed(storage, artifact_root)
            app = create_app(
                storage=storage,
                engine=Engine(storage, work_root=root / "work"),
                workspace_id=WORKSPACE,
                artifact_root=artifact_root,
                gateway=MockGateway({}),
            )
            worker_stop = threading.Event()
            worker_thread = threading.Thread(
                target=app.state.workflow_worker.run_forever,
                args=(app.state.workflow_actor,),
                kwargs={
                    "stop_event": worker_stop,
                    "poll_seconds": 0.05,
                    "worker_id": "browser-test-worker",
                },
                name="browser-test-worker",
                daemon=True,
            )
            worker_thread.start()
            try:
                uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
            finally:
                worker_stop.set()
                worker_thread.join(timeout=2)
        finally:
            if target_stub is not None:
                target_stub.shutdown()
                target_stub.server_close()
            if target_stub_thread is not None:
                target_stub_thread.join(timeout=2)
            if previous_allowlist is None:
                os.environ.pop("EVAL_ENGINE_ALLOW_ENDPOINTS", None)
            else:
                os.environ["EVAL_ENGINE_ALLOW_ENDPOINTS"] = previous_allowlist
            storage.close()


if __name__ == "__main__":
    main()
