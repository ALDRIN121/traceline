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


class R2Server:
    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status, body, content_type="application/json"):
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                self.rfile.read(length)
                owner.calls += 1
                if owner.mode == "stream":
                    self._send(200, b"event: token\ndata: hi\n\nevent: final\ndata: {\"answer\": \"ok\"}\n\n", "text/event-stream")
                else:
                    self._send(202, {"id": f"job-{owner.calls}"})

            def do_GET(self):
                self._send(200, {"state": "completed", "output": {"answer": "ok"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def allowlist(self):
        return f"127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_r2_stream_and_async_targets_run_through_the_frozen_worker_path(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_ENABLE_R2", "true")
    actor = Actor("owner", "ws", "owner")
    for mode in ("streaming", "async"):
        server = R2Server("stream" if mode == "streaming" else "async")
        server.start()
        monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", server.allowlist)
        storage = Storage(tmp_path / f"{mode}.db")
        storage.create_schema()
        try:
            project = storage.create_project(workspace_id="ws", name=mode)
            body = {"url": f"{server.base_url}/invoke", "mode": mode}
            if mode == "async":
                body["status_url_template"] = f"{server.base_url}/jobs/{{job_id}}"
            target = ConnectionService(storage, tmp_path / f"{mode}-secret.key").create(actor, project.project_id, body)
            verified = ConnectionService(storage, tmp_path / f"{mode}-secret.key").verify(
                actor, target["target_id"], {"target_version_id": target["version_id"], "smoke_input": {"q": "smoke"}},
            )
            versions = VersionStore(storage)
            evaluation = versions.create("evaluation", project.project_id, {"spec": {
                "spec_version": "1", "name": mode, "dataset_version": "v1",
                "cases": [{"case_id": "c1", "name": "one", "input": {"q": "run"}}],
                "metrics": [{
                    "metric_id": "answer", "name": "answer", "type": "scalar",
                    "target": {"type": "final_response", "selector": "$.answer", "on_missing": "fail"},
                    "evaluator": {"type": "exact_match", "expected": "ok"},
                    "scoring": {"type": "binary", "range": [0, 1]},
                    "aggregation": {"method": "pass_rate", "on_error": "fail"},
                    "gate": {"min": 1.0}, "provisional": False,
                }],
            }}, 0, actor)
            dataset = versions.create("dataset", project.project_id, {"cases": [
                {"case_id": "c1", "name": "one", "input": {"q": "run"}},
            ]}, 0, actor)
            worker = WorkflowWorker(storage, tmp_path / f"{mode}-artifacts", MockGateway({}), fingerprint_key=b"f" * 32)
            plans = RunPlanService(storage)
            plan = plans.plan_run(actor, {
                "project_id": project.project_id,
                "evaluation_version_id": evaluation.version_id,
                "dataset_version_id": dataset.version_id,
                "target_version_id": verified["target_version_id"],
            }, {"tier": "quick"})
            authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
            job = plans.enqueue_run(worker, actor, plan.plan_id, authorization.authorization_id,
                                    plan.content_digest, f"{mode}-once")
            terminal = worker.service(actor).run_once(job_id=job.job_id)
            assert terminal.status == "completed", terminal.error
            assert storage.get_run_metric_results(terminal.result["run_id"], "ws")[0].value == 1.0
            if mode == "streaming":
                events = storage.get_trace_events(run_id=terminal.result["run_id"], workspace_id="ws")
                assert [event.type.value for event in events] == [
                    "stream_start", "first_token", "llm_response"
                ]
                assert all(event.source.value == "adapter" for event in events)
            assert server.calls == 2
        finally:
            storage.close()
            server.stop()
