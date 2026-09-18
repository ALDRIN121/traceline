"""Process-level recovery for a frozen evaluation run."""

from __future__ import annotations

import json
import multiprocessing
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
import time
import uuid

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


PG_URL = os.environ.get("TEST_DATABASE_URL")


class HangingHostedTarget:
    def __init__(self):
        self.calls = 0
        self._lock = Lock()
        self.measured_started = Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                self.rfile.read(length)
                with owner._lock:
                    owner.calls += 1
                    call = owner.calls
                if call == 2:
                    owner.measured_started.set()
                    time.sleep(30)
                body = json.dumps({"answer": {"status": "ok"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/invoke"

    @property
    def allowlist(self):
        return f"127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _run_evaluation(database_url: str, artifact_root: str, workspace_id: str, job_id: str):
    storage = Storage(database_url)
    actor = Actor("worker-child", workspace_id, "owner")
    worker = WorkflowWorker(
        storage, Path(artifact_root), MockGateway({}), fingerprint_key=b"e" * 32,
        lease_seconds=2,
    )
    try:
        worker.service(actor, worker_id="evaluation-before").run_once(job_id=job_id)
    finally:
        storage.close()


@pytest.mark.integration
def test_killed_evaluation_worker_resumes_frozen_run_on_postgres(tmp_path, monkeypatch):
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for evaluation-run process recovery")
    target = HangingHostedTarget()
    target.start()
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", target.allowlist)
    workspace_id = f"evaluation_process_{uuid.uuid4().hex}"
    actor = Actor("owner", workspace_id, "owner")
    storage = Storage(PG_URL)
    storage.create_schema()
    try:
        project = storage.create_project(workspace_id=workspace_id, name="recovery")
        configured = ConnectionService(storage, tmp_path / "install-secret.key").create(
            actor, project.project_id,
            {"url": target.url, "output_mapping": {"final_response": "/answer"}, "timeout_seconds": 30},
        )
        verified = ConnectionService(storage, tmp_path / "install-secret.key").verify(
            actor, configured["target_id"],
            {"target_version_id": configured["version_id"], "smoke_input": {"q": "smoke"}},
        )
        versions = VersionStore(storage)
        evaluation = versions.create("evaluation", project.project_id, {"spec": {
            "spec_version": "1", "name": "recovery", "dataset_version": "v1",
            "cases": [{"case_id": "c1", "name": "one", "input": {"q": "run"}}],
            "metrics": [{
                "metric_id": "answer", "name": "answer", "type": "scalar",
                "target": {"type": "final_response", "selector": "$.status", "on_missing": "fail"},
                "evaluator": {"type": "exact_match", "expected": "ok"},
                "scoring": {"type": "binary", "range": [0, 1]},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "gate": {"min": 1.0}, "provisional": False,
            }],
        }}, 0, actor)
        dataset = versions.create("dataset", project.project_id, {
            "cases": [{"case_id": "c1", "name": "one", "input": {"q": "run"}}],
        }, 0, actor)
        worker = WorkflowWorker(
            storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"e" * 32,
            lease_seconds=2,
        )
        plans = RunPlanService(storage)
        plan = plans.plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
            "target_version_id": verified["target_version_id"],
        }, {"tier": "quick"})
        authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
        job = plans.enqueue_run(
            worker, actor, plan.plan_id, authorization.authorization_id,
            plan.content_digest, f"evaluation-recovery:{workspace_id}",
        )

        context = multiprocessing.get_context("spawn")
        child = context.Process(
            target=_run_evaluation,
            args=(PG_URL, str(tmp_path / "artifacts"), workspace_id, job.job_id),
        )
        child.start()
        try:
            assert target.measured_started.wait(15), "evaluation worker did not reach the hosted target"
            child.terminate()
            child.join(10)
            assert not child.is_alive()
        finally:
            if child.is_alive():
                child.kill()
                child.join(10)

        time.sleep(3)
        replacement = worker.service(actor, worker_id="evaluation-after").run_once()
        assert replacement.job_id == job.job_id
        assert replacement.status == "completed", replacement.error
        assert replacement.result["state"] == "complete"
        run_id = replacement.result["run_id"]
        run = storage.get_run(run_id, workspace_id)
        assert run.status == "complete"
        metric = storage.get_run_metric_results(run_id, workspace_id)[0]
        assert metric.gate_status == "PASS"
        assert target.calls >= 3  # verify, interrupted first attempt, resumed attempt
    finally:
        storage.close()
        target.stop()
