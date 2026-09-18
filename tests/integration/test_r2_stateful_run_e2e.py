from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class StatefulServer:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: dict):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.calls.append((self.path, body))
                if self.path == "/init":
                    self._send(200, {
                        "session_id": "session-1",
                        "state": "awaiting_input",
                        "requested_input": "Approve?",
                    })
                elif self.path == "/sessions/session-1/turns":
                    self._send(200, {"state": "completed", "output": {"answer": "approved"}})
                else:
                    self._send(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def allowlist(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_stateful_target_runs_multi_turn_case_through_worker_and_scores_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_ENABLE_R2", "true")
    server = StatefulServer()
    server.start()
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", server.allowlist)
    storage = Storage(tmp_path / "stateful.db")
    actor = Actor("owner", "ws", "owner")
    try:
        storage.create_schema()
        project = storage.create_project(workspace_id="ws", name="stateful")
        script = [
            {"kind": "wait_for_input", "requested_input": "Approve?"},
            {"kind": "user_response", "provided_input": "yes"},
        ]
        target = ConnectionService(storage, tmp_path / "secret.key").create(
            actor, project.project_id, {
                "url": f"{server.base_url}/init",
                "mode": "session",
                "turn_url_template": f"{server.base_url}/sessions/{{session_id}}/turns",
                "close_url_template": f"{server.base_url}/sessions/{{session_id}}/close",
            },
        )
        verified = ConnectionService(storage, tmp_path / "secret.key").verify(
            actor, target["target_id"], {
                "target_version_id": target["version_id"],
                "smoke_input": {"q": "verify", "interaction_script": script},
            },
        )
        assert verified["state"] == "verified", verified

        versions = VersionStore(storage)
        evaluation = versions.create("evaluation", project.project_id, {"spec": {
            "spec_version": "1", "name": "stateful", "dataset_version": "v1",
            "cases": [],
            "metrics": [{
                "metric_id": "answer", "name": "answer", "type": "scalar",
                "target": {"type": "final_response", "selector": "$.answer", "on_missing": "fail"},
                "evaluator": {"type": "exact_match", "expected": "approved"},
                "scoring": {"type": "binary", "range": [0, 1]},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "gate": {"min": 1.0}, "provisional": False,
            }],
        }}, 0, actor)
        dataset = versions.create("dataset", project.project_id, {"cases": [{
            "case_id": "c1", "name": "approval", "input": {"q": "refund"},
            "script": script,
        }]}, 0, actor)
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
        assert plan.state == "validated", plan.blockers
        authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
        job = plans.enqueue_run(
            worker, actor, plan.plan_id, authorization.authorization_id,
            plan.content_digest, "stateful-once",
        )
        terminal = worker.service(actor).run_once(job_id=job.job_id)

        assert terminal.status == "completed", terminal.error
        run_id = terminal.result["run_id"]
        assert storage.get_run_metric_results(run_id, "ws")[0].value == 1.0
        paths = [path for path, _body in server.calls]
        assert paths.count("/init") == 2  # verification and the actual run
        assert paths.count("/sessions/session-1/turns") == 2
        assert paths.count("/sessions/session-1/close") == 2
        assert all(event.type.value in {"wait_for_input", "user_response"}
                   for event in storage.get_trace_events(run_id=run_id, workspace_id="ws"))
    finally:
        storage.close()
        server.stop()
