from __future__ import annotations

import json
import io
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest

from llm_agent_eval.egress.ca import InstallCA
from llm_agent_eval.egress.listener import ProxyInstance
from llm_agent_eval.egress.recording import Budget, ProviderRoute
from llm_agent_eval.egress.session import ProxyRunSession
from llm_agent_eval.engine import Engine
from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.runtime.build import BuildService, RuntimeProfile
from llm_agent_eval.spec import validate_spec
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets.local import LocalTargetAdapter
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class _Provider(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.__class__.calls.append({"authorization": self.headers.get("authorization"), "body": body})
        if self.headers.get("authorization") != "Bearer real-provider-key":
            self.send_response(401)
            self.end_headers()
            return
        payload = {"ok": True, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


@pytest.mark.integration
@pytest.mark.live
def test_alpine_case_uses_managed_proxy_and_cannot_directly_egress(tmp_path: Path):
    if os.environ.get("LLM_AGENT_EVAL_RUN_PROXY_SANDBOX") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_PROXY_SANDBOX=1 for the live proxy/sandbox proof")
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    image = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", "docker.io/library/alpine:3.20"],
        check=False, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not image:
        pytest.skip("the pinned Alpine image is not available locally")

    _Provider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_port
    route = ProviderRoute(
        # The route authority is resolved only on the trusted host-side
        # provider leg. The case reaches the listener through the internal
        # relay; a production DNS-all-names-to-proxy test remains a separate
        # platform gate.
        host=f"host.containers.internal:{port}", origin=f"http://127.0.0.1:{port}",
        path="/v1/chat/completions", model="fixture-model", provider="openai",
        secret_ref="provider", dummy_key="dummy-case-key", max_input_tokens=10,
        max_output_tokens=2, input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 1,
    )
    source = tmp_path / "source"
    source.mkdir()
    records: list[dict] = []
    session = ProxyRunSession(
        run_id="live-proxy-run", install_root=tmp_path / "install", routes=[route],
        budget=Budget(1000), secret_resolver=lambda _ref: "real-provider-key",
        record=records.append,
    )
    try:
        info = session.start()
        relay_host = urlsplit(info.proxy_endpoint).hostname
        assert relay_host is not None
        (source / "agent.sh").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "body='{" + '"model":"fixture-model","max_tokens":1,"messages":[]' + "}'\n"
            "{\n"
            f"  printf 'POST http://host.containers.internal:{port}/v1/chat/completions HTTP/1.1\\r\\n'\n"
            f"  printf 'Host: host.containers.internal:{port}\\r\\n'\n"
            "  printf 'Authorization: Bearer dummy-case-key\\r\\n'\n"
            "  printf 'Content-Type: application/json\\r\\n'\n"
            "  printf 'Content-Length: %s\\r\\nConnection: close\\r\\n\\r\\n' \"${#body}\"\n"
            "  printf '%s' \"$body\"\n"
            f"}} | nc -w 5 {relay_host} 3128 > /tmp/provider.response\n"
            "grep -q '\"usage\"' /tmp/provider.response\n"
            "if nc -w 2 203.0.113.1 81 </dev/null >/dev/null 2>&1; then\n"
            "  exit 12\n"
            "fi\n"
            "printf '{\"answer\":\"ok\"}' > /output/result.json\n",
            encoding="utf-8",
        )
        result = LocalTargetAdapter().invoke(
            {"image": image[0], "entrypoint": ["/bin/sh", "/source/agent.sh"],
             "timeout_seconds": 30, "egress": "proxy"},
            {"question": "hello"},
            {"run_id": "live-proxy-run", "workspace_id": "live-ws", "case_id": "live-case",
             "attempt_id": "live-attempt", "proxy_endpoint": info.proxy_endpoint,
             "network_name": info.network_name, "network_run_id": info.network_run_id,
             "ca_cert": info.ca_cert, "source_dir": str(source)},
        )
        assert result.outcome == "ok", {"result": result, "records": records}
        assert result.output == {"answer": "ok"}
        assert len(_Provider.calls) == 1
        assert _Provider.calls[0]["authorization"] == "Bearer real-provider-key"
        assert records[-1]["source"] == "proxy"
        assert records[-1]["cost_usd_micros"] == 5
        assert session.ca is not None
        assert session.ca.directory != Path(__file__).parents[2]
    finally:
        session.stop()
        upstream.shutdown()


@pytest.mark.integration
def test_proxy_connect_terminates_tls_with_install_ca(tmp_path: Path):
    _Provider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_port
    route = ProviderRoute(
        host=f"provider.test:{port}", origin=f"http://127.0.0.1:{port}",
        path="/v1/chat/completions", model="fixture-model", provider="openai",
        secret_ref="provider", dummy_key="dummy-case-key", max_input_tokens=10,
        max_output_tokens=2, input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 1,
    )
    records: list[dict] = []
    ca = InstallCA.load_or_create(tmp_path / "install")
    proxy = ProxyInstance(
        [route], Budget(1000), lambda _ref: "real-provider-key", records.append,
        ca=ca,
    )
    endpoint = proxy.start()
    try:
        import httpx
        import ssl
        tls_context = ssl.create_default_context(cafile=ca.directory / "interception-ca.pem")
        with httpx.Client(proxy=endpoint.url, verify=tls_context, trust_env=False) as client:
            response = client.post(
                f"https://provider.test:{port}/v1/chat/completions",
                headers={"Authorization": "Bearer dummy-case-key"},
                json={"model": "fixture-model", "max_tokens": 1, "messages": []},
            )
        assert response.status_code == 200
        assert _Provider.calls[0]["authorization"] == "Bearer real-provider-key"
        assert records[-1]["cost_usd_micros"] == 5
    finally:
        proxy.stop()
        upstream.shutdown()


@pytest.mark.integration
@pytest.mark.live
def test_engine_persists_scored_local_proxy_run(tmp_path: Path):
    if os.environ.get("LLM_AGENT_EVAL_RUN_PROXY_SANDBOX") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_PROXY_SANDBOX=1 for the live engine proof")
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    image = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", "docker.io/library/alpine:3.20"],
        check=False, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not image:
        pytest.skip("the pinned Alpine image is not available locally")

    _Provider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_port
    route = ProviderRoute(
        host=f"host.containers.internal:{port}", origin=f"http://127.0.0.1:{port}",
        path="/v1/chat/completions", model="fixture-model", provider="openai",
        secret_ref="provider", dummy_key="dummy-case-key", max_input_tokens=10,
        max_output_tokens=2, input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 1,
    )
    source = tmp_path / "source"
    source.mkdir()
    storage = Storage(tmp_path / "engine.db")
    storage.create_schema()
    records: list[dict] = []
    session = ProxyRunSession(
        run_id="engine-proxy-run", install_root=tmp_path / "install", routes=[route],
        budget=Budget(1000), secret_resolver=lambda _ref: "real-provider-key",
        record=records.append,
    )
    try:
        info = session.start()
        relay_host = urlsplit(info.proxy_endpoint).hostname
        assert relay_host is not None
        (source / "agent.sh").write_text(
            "#!/bin/sh\nset -eu\n"
            "body='{" + '"model":"fixture-model","max_tokens":1,"messages":[]' + "}'\n"
            "{\n"
            f"  printf 'POST http://host.containers.internal:{port}/v1/chat/completions HTTP/1.1\\r\\n'\n"
            f"  printf 'Host: host.containers.internal:{port}\\r\\n'\n"
            "  printf 'Authorization: Bearer dummy-case-key\\r\\n'\n"
            "  printf 'Content-Type: application/json\\r\\n'\n"
            "  printf 'Content-Length: %s\\r\\nConnection: close\\r\\n\\r\\n' \"${#body}\"\n"
            "  printf '%s' \"$body\"\n"
            f"}} | nc -w 5 {relay_host} 3128 > /tmp/provider.response\n"
            "grep -q '\"usage\"' /tmp/provider.response\n"
            "printf '{\"answer\":\"ok\"}' > /output/result.json\n",
            encoding="utf-8",
        )
        built = BuildService().prepare(
            source, RuntimeProfile(
                "sha256:" + image[0], ("/bin/sh", "/source/agent.sh")
            ),
        )
        spec = validate_spec({
            "spec_version": "1", "name": "live-local", "dataset_version": "v1",
            "cases": [{"case_id": "c1", "name": "one", "input": {"question": "hello"}}],
            "metrics": [{
                "metric_id": "answer", "name": "answer", "type": "scalar",
                "target": {"type": "final_response", "selector": "$.answer", "on_missing": "fail"},
                "evaluator": {"type": "exact_match", "expected": "ok"},
                "scoring": {"type": "binary", "range": [0, 1]},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "gate": {"min": 1.0}, "provisional": False,
            }],
        })
        engine = Engine(
            storage, target_adapter=LocalTargetAdapter(),
            target_execution_context={
                "source_dir": str(source), "proxy_endpoint": info.proxy_endpoint,
                "network_name": info.network_name, "network_run_id": info.network_run_id,
                "ca_cert": info.ca_cert,
            }, work_root=tmp_path / "work",
        )
        run = engine.create_run(
            "live-ws", spec, tier="quick", entrypoint=("/bin/sh", "/source/agent.sh"),
            invocation_manifest={
                "image": built.image_digest, "entrypoint": list(built.entrypoint),
                "timeout_seconds": 30, "egress": "proxy",
            }, budget_usd_micros=1000,
        )
        result = engine.run(run.run_id, "live-ws")
        assert result.status == "complete"
        metric = storage.get_run_metric_results(run.run_id, "live-ws")[0]
        assert metric.value == 1.0
        assert _Provider.calls[0]["authorization"] == "Bearer real-provider-key"
        assert records[-1]["cost_usd_micros"] == 5
    finally:
        session.stop()
        storage.close()
        upstream.shutdown()


@pytest.mark.integration
@pytest.mark.live
def test_authorized_worker_run_completes_import_build_proxy_and_scoring(tmp_path: Path):
    if os.environ.get("LLM_AGENT_EVAL_RUN_PROXY_SANDBOX") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_PROXY_SANDBOX=1 for the live workflow proof")
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    image = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", "docker.io/library/alpine:3.20"],
        check=False, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not image:
        pytest.skip("the pinned Alpine image is not available locally")

    _Provider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_port
    route = ProviderRoute(
        host=f"host.containers.internal:{port}", origin=f"http://127.0.0.1:{port}",
        path="/v1/chat/completions", model="fixture-model", provider="openai",
        secret_ref="provider", dummy_key="dummy-case-key", max_input_tokens=10,
        max_output_tokens=2, input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 1,
    )
    storage = Storage(tmp_path / "worker-live.db")
    storage.create_schema()
    actor = Actor("owner", "live-ws", "owner")
    records: list[dict] = []

    def proxy_factory(_actor, run_id, plan, _context):
        return ProxyRunSession(
            run_id=run_id, install_root=tmp_path / "install", routes=[route],
            budget=Budget(1000),
            secret_resolver=lambda _ref: "real-provider-key", record=records.append,
        )

    try:
        project = storage.create_project(workspace_id="live-ws", name="worker-live")
        archive = io.BytesIO()
        with ZipFile(archive, "w") as zipped:
            zipped.writestr(
                "agent.sh",
                "#!/bin/sh\nset -eu\n"
                "proxy=${HTTP_PROXY#http://}\n"
                "proxy_host=${proxy%:*}\n"
                "proxy_port=${proxy##*:}\n"
                "body='{\"model\":\"fixture-model\",\"max_tokens\":1,\"messages\":[]}'\n"
                "{\n"
                f"  printf 'POST http://host.containers.internal:{port}/v1/chat/completions HTTP/1.1\\r\\n'\n"
                f"  printf 'Host: host.containers.internal:{port}\\r\\n'\n"
                "  printf 'Authorization: Bearer dummy-case-key\\r\\n'\n"
                "  printf 'Content-Type: application/json\\r\\n'\n"
                "  printf 'Content-Length: %s\\r\\nConnection: close\\r\\n\\r\\n' \"${#body}\"\n"
                "  printf '%s' \"$body\"\n"
                "} | nc -w 5 \"$proxy_host\" \"$proxy_port\" > /tmp/provider.response\n"
                "grep -q '\"usage\"' /tmp/provider.response\n"
                "printf '{\"answer\":\"ok\"}' > /output/result.json\n",
            )
        artifact = ArtifactStore(storage, tmp_path / "artifacts", actor).put(
            "live-ws", archive.getvalue(), "application/zip"
        )
        versions = VersionStore(storage)
        source = versions.create(
            "source", project.project_id,
            {"artifact_ids": [artifact.artifact_id], "readiness": "blocked", "manifest": {}},
            0, actor,
        )
        worker = WorkflowWorker(
            storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
            proxy_session_factory=proxy_factory,
        )
        prepare = worker.queue(actor).enqueue({
            "kind": "source_prepare", "source_version_id": source.version_id,
            "runtime_profile": {
                "base_image": "sha256:" + image[0],
                "entrypoint": ["/bin/sh", "/source/agent.sh"],
                "egress": "proxy",
            },
        }, "prepare-live")
        prepared = worker.service(actor).run_once(job_id=prepare.job_id)
        assert prepared.status == "completed", prepared.error
        source_version_id = prepared.result["source_version_id"]
        evaluation = versions.create("evaluation", project.project_id, {"spec": {
            "spec_version": "1", "name": "worker-live", "dataset_version": "v1",
            "cases": [{"case_id": "c1", "name": "one", "input": {"question": "hello"}}],
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
            {"case_id": "c1", "name": "one", "input": {"question": "hello"}},
        ]}, 0, actor)
        plans = RunPlanService(storage)
        plan = plans.plan_run(actor, {
            "project_id": project.project_id,
            "source_version_id": source_version_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        }, {"tier": "quick", "budget_usd_micros": 1000})
        assert plan.state == "validated"
        authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
        run_job = plans.enqueue_run(
            worker, actor, plan.plan_id, authorization.authorization_id,
            plan.content_digest, "worker-live-run",
        )
        terminal = worker.service(actor).run_once(job_id=run_job.job_id)
        assert terminal.status == "completed", terminal.error
        assert terminal.result["state"] == "complete"
        assert storage.get_run_metric_results(terminal.result["run_id"], "live-ws")[0].value == 1.0
        events = storage.get_trace_events(run_id=terminal.result["run_id"], workspace_id="live-ws")
        assert any(event.source.value == "proxy" for event in events)
        assert storage.get_cost_summaries(terminal.result["run_id"], "live-ws")[0]["usd_micros"] == 5
        assert _Provider.calls[0]["authorization"] == "Bearer real-provider-key"
        assert records[-1]["cost_usd_micros"] == 5
    finally:
        storage.close()
        upstream.shutdown()
