"""Opt-in live proof for the embedded LiteLLM HTTPS outbound seam."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from llm_agent_eval.egress.ca import InstallCA
from llm_agent_eval.egress.transport import ProviderTransport


class _Provider(BaseHTTPRequestHandler):
    calls: list[dict[str, object]] = []

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.__class__.calls.append({
            "path": self.path,
            "authorization": self.headers.get("authorization"),
            "body": body,
        })
        if body.get("stream") is True:
            frames = (
                b'data: {"choices":[{"delta":{"content":"local"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" stream"}}]}\n\n'
                b'data: {"choices":[], '
                b'"usage":{"prompt_tokens":2,"completion_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(frames)))
            self.end_headers()
            self.wfile.write(frames)
            return
        payload = json.dumps({
            "choices": [{"message": {"content": "local provider response"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.mark.integration
@pytest.mark.live
def test_embedded_litellm_router_reaches_pinned_local_https_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    if os.environ.get("LLM_AGENT_EVAL_RUN_LITELLM_PROVIDER") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_LITELLM_PROVIDER=1 for the LiteLLM HTTPS proof")

    ca = InstallCA.load_or_create(tmp_path / "install")
    _Provider.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    upstream.socket = ca.context_for("127.0.0.1").wrap_socket(
        upstream.socket, server_side=True,
    )
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca.directory / "interception-ca.pem"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca.directory / "interception-ca.pem"))
    port = upstream.server_port
    route = {
        "provider": "openai",
        "model": "fixture-model",
        "origin": f"https://127.0.0.1:{port}/v1",
        "path": "/v1/chat/completions",
    }
    try:
        status, payload = ProviderTransport()(route, {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
        }, {"Authorization": "Bearer real-provider-key"})
        stream_status, stream = ProviderTransport()(route, {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        }, {"Authorization": "Bearer real-provider-key"})
        stream_body = b"".join(stream)
    finally:
        upstream.shutdown()

    assert status == 200
    assert payload["choices"][0]["message"]["content"] == "local provider response"
    assert payload["usage"]["prompt_tokens"] == 2
    assert payload["usage"]["completion_tokens"] == 1
    assert _Provider.calls[0] == {
        "path": "/v1/chat/completions",
        "authorization": "Bearer real-provider-key",
        "body": {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    }
    assert stream_status == 200
    assert b'"content":"local"' in stream_body
    assert b'"content":" stream"' in stream_body
    assert b'"completion_tokens":2' in stream_body
    assert stream_body.endswith(b"data: [DONE]\n\n")
    assert _Provider.calls[1]["body"]["stream"] is True
