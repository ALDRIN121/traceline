"""Conformance for the tested provider-envelope R2 adapters."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from llm_agent_eval.auth import Actor
from llm_agent_eval.storage import Storage
from llm_agent_eval.targets import ConnectionService
from llm_agent_eval.targets.http_provider import (
    AnthropicMessagesAdapter,
    GoogleGenerativeAdapter,
)
from llm_agent_eval.targets.network_policy import EndpointPolicy
from llm_agent_eval.targets.frameworks import adapter_capability, supported_adapters
from llm_agent_eval.versions import VersionStore


def _server(response, received):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_anthropic_messages_adapter_maps_messages_and_text_response():
    received = []
    server = _server({"content": [{"type": "text", "text": "ok"}]}, received)
    url = f"http://127.0.0.1:{server.server_port}/v1/messages"
    allow = f"127.0.0.1:{server.server_port}"
    target = {
        "url": url, "method": "POST", "model": "claude-test", "max_tokens": 32,
        "auth": {"type": "none"}, "output_mapping": {"final_response": "/answer"},
        "timeout_seconds": 5, "max_response_bytes": 1024,
        "pinned": EndpointPolicy(allow_exact={allow}).authorize(url),
    }
    try:
        result = AnthropicMessagesAdapter(EndpointPolicy(allow_exact={allow})).invoke(
            {"target": target}, {"q": "hello"}, {"workspace_id": "ws"},
        )
        assert result.outcome == "ok"
        assert result.output == {"final_response": "ok"}
        assert received == [{
            "model": "claude-test", "max_tokens": 32,
            "messages": [{"role": "user", "content": '{"q":"hello"}'}],
        }]
    finally:
        server.shutdown()
        server.server_close()


def test_google_generative_adapter_maps_contents_and_text_response():
    received = []
    server = _server({
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
    }, received)
    url = f"http://127.0.0.1:{server.server_port}/v1beta/models/test:generateContent"
    allow = f"127.0.0.1:{server.server_port}"
    target = {
        "url": url, "method": "POST", "model": "test", "max_tokens": 32,
        "auth": {"type": "none"}, "output_mapping": {"final_response": "/answer"},
        "timeout_seconds": 5, "max_response_bytes": 1024,
        "pinned": EndpointPolicy(allow_exact={allow}).authorize(url),
    }
    try:
        result = GoogleGenerativeAdapter(EndpointPolicy(allow_exact={allow})).invoke(
            {"target": target}, {"q": "hello"}, {"workspace_id": "ws"},
        )
        assert result.outcome == "ok"
        assert result.output == {"final_response": "ok"}
        assert received == [{
            "contents": [{"role": "user", "parts": [{"text": '{"q":"hello"}'}]}],
            "generationConfig": {"maxOutputTokens": 32},
        }]
    finally:
        server.shutdown()
        server.server_close()


def test_provider_envelope_adapters_are_explicitly_conformant():
    matrix = supported_adapters()
    for framework in ("anthropic_messages", "google_generative"):
        capability = adapter_capability(framework, version="stateless_json", require_supported=True)
        assert capability in matrix
        assert capability.capabilities["final_output"] == "observed"
        assert capability.conformance_tests == ("tests/workflow/test_provider_adapters.py",)


def test_connection_service_selects_provider_envelope_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_ALLOW_ENDPOINTS", "127.0.0.1:9")
    storage = Storage(tmp_path / "provider-adapters.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        project = storage.create_project(workspace_id="ws", name="provider adapters")
        service = ConnectionService(storage, tmp_path / "secret.key")
        for framework, model, expected in (
            ("anthropic_messages", "claude-test", AnthropicMessagesAdapter),
            ("google_generative", "gemini-test", GoogleGenerativeAdapter),
        ):
            created = service.create(actor, project.project_id, {
                "url": "http://127.0.0.1:9/invoke", "framework": framework,
                "model": model, "max_tokens": 32,
            })
            version = VersionStore(storage).get(created["version_id"], actor)
            assert version.content["framework"] == framework
            assert version.content["model"] == model
            assert isinstance(ConnectionService._adapter(version.content), expected)
    finally:
        storage.close()
