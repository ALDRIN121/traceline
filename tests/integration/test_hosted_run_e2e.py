from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient

from llm_agent_eval.auth import Actor
from llm_agent_eval.api import create_app
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.rescore import RescoreService
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class Agent:
    def __init__(self, response=None):
        self.calls = 0
        self.response = response or {"answer": {"status": "ok"}}
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                self.rfile.read(length)
                agent.calls += 1
                body = json.dumps(agent.response).encode()
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


def test_authorized_hosted_run_scores_observed_output_without_trace(tmp_path, monkeypatch):
    agent = Agent()
    agent.start()
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", agent.allowlist)
    storage = Storage(tmp_path / "hosted.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        project = storage.create_project(workspace_id="ws", name="hosted")
        target = ConnectionService(storage, tmp_path / "install-secret.key").create(
            actor, project.project_id,
            {"url": agent.url, "output_mapping": {"final_response": "/answer"}},
        )
        verified = ConnectionService(storage, tmp_path / "install-secret.key").verify(
            actor, target["target_id"], {"target_version_id": target["version_id"], "smoke_input": {"q": "smoke"}},
        )
        versions = VersionStore(storage)
        evaluation = versions.create("evaluation", project.project_id, {"spec": {
            "spec_version": "1", "name": "hosted", "dataset_version": "v1",
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
        dataset = versions.create("dataset", project.project_id, {"cases": [
            {"case_id": "c1", "name": "one", "input": {"q": "run"}},
        ]}, 0, actor)
        worker = WorkflowWorker(
            storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
        )
        plans = RunPlanService(storage)
        plan = plans.plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
            "target_version_id": verified["target_version_id"],
        }, {"tier": "quick"})
        assert plan.state == "validated"
        authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
        job = plans.enqueue_run(
            worker, actor, plan.plan_id, authorization.authorization_id,
            plan.content_digest, "hosted-once",
        )
        terminal = worker.service(actor).run_once(job_id=job.job_id)
        assert terminal.status == "completed", (terminal.error, terminal.result)
        assert terminal.result["state"] == "complete"
        assert agent.calls == 2  # one verification smoke plus one measured case
        metric = storage.get_run_metric_results(terminal.result["run_id"], "ws")[0]
        assert metric.value == 1.0
        assert metric.gate_status == "PASS"
        client = TestClient(create_app(
            storage=storage, workspace_id="ws", artifact_root=tmp_path / "artifacts",
            gateway=MockGateway({}),
        ))
        frozen = client.post("/api/exports", json={"run_id": terminal.result["run_id"]})
        assert frozen.status_code == 201
        export_id = frozen.json()["export_id"]
        manifest = client.get(f"/api/exports/{export_id}/json")
        assert manifest.status_code == 200
        assert manifest.json()["run"]["run_id"] == terminal.result["run_id"]
        assert manifest.json()["case_metrics"][0]["metric_id"] == "answer"
        assert client.get(f"/api/exports/{export_id}/csv").status_code == 200
        assert client.get(f"/api/exports/{export_id}/html").status_code == 200
        ci = client.get(f"/ci/runs/{terminal.result['run_id']}")
        assert ci.status_code == 200
        assert ci.json()["exit_code"] == 2  # one repeat is complete but has no CI confidence interval
        replacement = {
            "metric_id": "answer", "name": "answer changed", "type": "scalar",
            "target": {"type": "final_response", "selector": "$.status", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "not-ok"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0}, "provisional": False,
        }
        rescored = RescoreService(storage).rescore(
            actor, terminal.result["run_id"], "answer", replacement, 2,
        )
        assert rescored["state"] == "rescored"
        assert rescored["aggregate"]["value"] == 0.0
        assert agent.calls == 2  # re-score used retained evidence only
    finally:
        storage.close()
        agent.stop()


def test_authorized_hosted_run_persists_retrieval_event_and_scores(tmp_path, monkeypatch):
    agent = Agent({
        "answer": {"status": "ok"},
        "retrieval": {
            "query": "what?",
            "chunks": ["doc-b", "doc-a"],
            "scores": [0.9, 0.8],
            "source": "index-v1",
        },
    })
    agent.start()
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", agent.allowlist)
    storage = Storage(tmp_path / "hosted-retrieval.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        project = storage.create_project(workspace_id="ws", name="hosted-retrieval")
        target = ConnectionService(storage, tmp_path / "install-secret.key").create(
            actor, project.project_id,
            {
                "url": agent.url,
                "output_mapping": {"final_response": "/answer"},
                "retrieval_mapping": {
                    "query": "/retrieval/query",
                    "chunks": "/retrieval/chunks",
                    "scores": "/retrieval/scores",
                    "source": "/retrieval/source",
                },
            },
        )
        verified = ConnectionService(storage, tmp_path / "install-secret.key").verify(
            actor, target["target_id"],
            {"target_version_id": target["version_id"], "smoke_input": {"q": "smoke"}},
        )
        versions = VersionStore(storage)
        spec = {
            "spec_version": "1", "name": "hosted-retrieval", "dataset_version": "v1",
            "cases": [{"case_id": "c1", "name": "one", "input": {"q": "run"}}],
            "metrics": [{
                "metric_id": "recall", "name": "recall", "type": "scalar",
                "target": {"type": "retrieval", "selector": "$.chunks", "on_missing": "fail"},
                "evaluator": {"type": "recall_at_k", "expected": ["doc-a", "doc-b"], "k": 2},
                "scoring": {"type": "numeric", "range": [0, 1]},
                "aggregation": {"method": "mean", "on_error": "fail"},
                "gate": {"min": 1.0}, "provisional": False,
            }],
        }
        evaluation = versions.create("evaluation", project.project_id, {"spec": spec}, 0, actor)
        dataset = versions.create("dataset", project.project_id, {"cases": [
            {"case_id": "c1", "name": "one", "input": {"q": "run"}},
        ]}, 0, actor)
        worker = WorkflowWorker(
            storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
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
            plan.content_digest, "hosted-retrieval-once",
        )
        terminal = worker.service(actor).run_once(job_id=job.job_id)
        assert terminal.status == "completed", (terminal.error, terminal.result)
        assert storage.get_run_metric_results(terminal.result["run_id"], "ws")[0].value == 1.0
        events = storage.get_trace_events(run_id=terminal.result["run_id"], workspace_id="ws")
        retrieval = [event for event in events if event.type.value == "retrieval"]
        assert len(retrieval) == 1
        assert retrieval[0].source.value == "adapter"
        assert retrieval[0].payload["chunks"] == ["doc-b", "doc-a"]
        assert agent.calls == 2
    finally:
        storage.close()
        agent.stop()
