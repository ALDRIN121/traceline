"""Conformance for the tested OpenAI-compatible stateless JSON adapter."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from llm_agent_eval.auth import Actor
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.targets.http_openai import OpenAICompatibleAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy
from llm_agent_eval.targets.frameworks import adapter_capability, supported_adapters
from llm_agent_eval.versions import VersionStore


def test_openai_compatible_adapter_maps_messages_and_response_content():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            body = json.dumps({
                "id": "chatcmpl-test",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    allow = f"127.0.0.1:{server.server_port}"
    target = {
        "url": url,
        "method": "POST",
        "auth": {"type": "none"},
        "output_mapping": {"final_response": "/answer"},
        "timeout_seconds": 5,
        "max_response_bytes": 1024,
        "pinned": EndpointPolicy(allow_exact={allow}).authorize(url),
    }
    try:
        result = OpenAICompatibleAdapter(EndpointPolicy(allow_exact={allow})).invoke(
            {"target": target}, {"q": "hello"}, {"workspace_id": "ws"},
        )
        assert result.outcome == "ok"
        assert result.output == {"final_response": "ok"}
        assert received == [{"messages": [{"role": "user", "content": '{"q":"hello"}'}]}]
    finally:
        server.shutdown()
        server.server_close()


def test_openai_compatible_adapter_is_explicitly_conformant():
    matrix = supported_adapters()
    capability = adapter_capability("openai_compatible", version="stateless_json", require_supported=True)
    assert capability in matrix
    assert capability.capabilities["final_output"] == "observed"
    assert capability.conformance_tests == ("tests/workflow/test_openai_compatible_adapter.py",)


def test_connection_service_selects_the_declared_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", "127.0.0.1:9")
    storage = Storage(tmp_path / "adapter.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        project = storage.create_project(workspace_id="ws", name="adapter")
        created = ConnectionService(storage, tmp_path / "secret.key").create(
            actor,
            project.project_id,
            {"url": "http://127.0.0.1:9/invoke", "framework": "openai_compatible"},
        )
        assert created["state"] == "configured"
        version = VersionStore(storage).get(created["version_id"], actor)
        assert version.content["framework"] == "openai_compatible"
        assert isinstance(ConnectionService._adapter(version.content), OpenAICompatibleAdapter)
    finally:
        storage.close()
