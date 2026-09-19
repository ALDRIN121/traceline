"""Opt-in live HTTPS-client proof for the managed case proxy."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from llm_agent_eval.egress.recording import Budget, ProviderRoute
from llm_agent_eval.egress.session import ProxyRunSession
from llm_agent_eval.runtime.sandbox import PodmanSandbox
from llm_agent_eval.targets.local import LocalTargetAdapter


class _HTTPSFixtureProvider(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.__class__.calls.append({
            "authorization": self.headers.get("authorization"),
            "body": body,
        })
        payload = json.dumps({"ok": True, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class _ObservedSandbox(PodmanSandbox):
    def run(self, request, **kwargs):
        self.last_result = super().run(request, **kwargs)
        return self.last_result


@pytest.mark.integration
@pytest.mark.live
def test_live_case_https_client_uses_install_ca_and_proxy(tmp_path: Path):
    if os.environ.get("LLM_AGENT_EVAL_RUN_PROXY_HTTPS") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_PROXY_HTTPS=1 for the HTTPS case proof")
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    image_ref = os.environ.get("LLM_AGENT_EVAL_HTTPS_CLIENT_IMAGE", "docker.io/curlimages/curl:8.11.1")
    image_ids = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", image_ref],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    if not image_ids:
        pytest.skip("the pinned HTTPS-client image is not available locally")

    _HTTPSFixtureProvider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPSFixtureProvider)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_port
    route = ProviderRoute(
        host=f"provider.test:{port}",
        origin=f"http://127.0.0.1:{port}",
        path="/v1/chat/completions",
        model="fixture-model",
        provider="openai",
        secret_ref="provider",
        dummy_key="dummy-case-key",
        max_input_tokens=10,
        max_output_tokens=2,
        input_micros_per_token=2,
        output_micros_per_token=3,
        price_version="fixture-v1",
        input_token_bound=lambda _body: 1,
    )
    records: list[dict] = []
    session = ProxyRunSession(
        run_id="live-https-case",
        install_root=tmp_path / "install",
        routes=[route],
        budget=Budget(1000),
        secret_resolver=lambda _ref: "real-provider-key",
        record=records.append,
    )
    source = tmp_path / "source"
    source.mkdir()
    sandbox = _ObservedSandbox()
    try:
        info = session.start()
        (source / "agent.sh").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "body='{" + '"model":"fixture-model","max_tokens":1,"messages":[]' + "}'\n"
            f"curl --fail --silent --show-error --output /tmp/provider.response "
            f"--header 'Authorization: Bearer dummy-case-key' "
            f"--header 'Content-Type: application/json' "
            f"--data \"$body\" "
            f"https://provider.test:{port}/v1/chat/completions\n"
            "grep -q 'usage' /tmp/provider.response\n"
            "printf '{\"answer\":\"ok\"}' > /output/result.json\n",
            encoding="utf-8",
        )
        result = LocalTargetAdapter(sandbox=sandbox).invoke(
            {
                "image": image_ids[0],
                "entrypoint": ["/bin/sh", "/source/agent.sh"],
                "timeout_seconds": 30,
                "egress": "proxy",
            },
            {"question": "hello"},
            {
                "run_id": "live-https-case",
                "workspace_id": "live-ws",
                "case_id": "live-case",
                "attempt_id": "live-attempt",
                "proxy_endpoint": info.proxy_endpoint,
                "network_name": info.network_name,
                "network_run_id": info.network_run_id,
                "ca_cert": info.ca_cert,
                "source_dir": str(source),
            },
        )
        assert result.outcome == "ok", json.dumps({
            "result": result,
            "sandbox": getattr(sandbox, "last_result", None),
            "stderr": getattr(sandbox.last_result, "stderr_tail", None),
            "stdout": getattr(sandbox.last_result, "stdout_tail", None),
            "records": records,
            "provider_calls": _HTTPSFixtureProvider.calls,
        }, default=str)
        assert result.output == {"answer": "ok"}
        assert _HTTPSFixtureProvider.calls[0]["authorization"] == "Bearer real-provider-key"
        assert records[-1]["cost_usd_micros"] == 5
    finally:
        session.stop()
        upstream.shutdown()
