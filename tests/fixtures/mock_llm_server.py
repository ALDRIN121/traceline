#!/usr/bin/env python3
"""A tiny OpenAI-compatible chat completions server for offline agent runs.

The engine's runs never call out (engine invariant: no network) — the agent
under test is the only subprocess. This server stands in for DeepSeek during
offline runs: the sample agent points its LLM client at the server's base URL
(``LLM_AGENT_EVAL_LLM_BASE_URL``) and gets a canned, keyword-aware
classification plus a usage block, so cost flows end-to-end.

Contract (the OpenAI-compatible subset the sample agent and the platform's
DeepSeek gateway share)::

    POST /chat/completions
      request:  {"model": str, "messages": [{"role", "content"}, ...]}
      response: {
        "id": "chatcmpl-mock-<n>", "object": "chat.completion",
        "model": <requested model or "mock-llm-1">, "created": <epoch s>,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": <canned JSON>},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 14, "completion_tokens": 9,
                  "total_tokens": 23}
      }

The canned content is a triage verdict: any user message containing "refund"
classifies as a refund request, anything else as a question. Usage is
deterministic so cost summaries are reproducible. Every request body is
recorded on the server (``.requests``) in arrival order for assertions.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "mock-llm-1"
USAGE_PROMPT_TOKENS = 14
USAGE_COMPLETION_TOKENS = 9


def classify(user_messages: list[Any]) -> dict[str, str]:
    text = " ".join(
        (m.get("content") or "") for m in user_messages if isinstance(m, dict)
    ).lower()
    if "refund" in text:
        return {"intent": "refund", "summary": "Customer requests a refund."}
    return {"intent": "question", "summary": "Customer asks a general question."}


def completion_payload(model: str, content: str, request_no: int) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-mock-{request_no}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": USAGE_PROMPT_TOKENS,
            "completion_tokens": USAGE_COMPLETION_TOKENS,
            "total_tokens": USAGE_PROMPT_TOKENS + USAGE_COMPLETION_TOKENS,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/chat/completions":
            self.send_error(404, f"unknown endpoint {self.path}")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, TypeError) as exc:
            self.send_error(400, f"malformed request body: {exc}")
            return
        if not isinstance(body, dict):
            self.send_error(400, "request body must be a JSON object")
            return
        server = self.server
        request_no = server.record_request(body)  # type: ignore[attr-defined]
        model = body.get("model") or MODEL
        content = json.dumps(classify(body.get("messages") or []))
        payload = completion_payload(model, content, request_no)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # quieter logs
        return


class MockLLMServer:
    """A chat-completions server on an ephemeral localhost port; run it in a
    daemon thread. ``requests`` records every request body in arrival order."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        # The handler reaches the recorder through its httpd (`self.server`).
        self._httpd.record_request = self.record_request  # type: ignore[attr-defined]
        self.model = MODEL
        self.requests: list[dict[str, Any]] = []
        self._request_no = 0
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def record_request(self, body: dict[str, Any]) -> int:
        self._request_no += 1
        self.requests.append(dict(body))
        return self._request_no

    def start(self) -> "MockLLMServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "MockLLMServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
