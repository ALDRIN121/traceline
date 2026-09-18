"""Typed hosted-target outcomes without inventing success from health or redirects."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

from llm_agent_eval.targets.http_json import HttpJsonAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy
from llm_agent_eval.events import EventType, Source


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/invoke", f"{host}:{port}"


def _target(url, **extra):
    content = {
        "url": url, "method": "POST", "auth": {"type": "none"},
        "output_mapping": {"final_response": "/answer"}, "timeout_seconds": extra.pop("timeout_seconds", 5),
        "max_response_bytes": extra.pop("max_response_bytes", 1024),
    }
    content.update(extra)
    return content


def _invoke(url, allow, target=None, smoke=None):
    adapter = HttpJsonAdapter(EndpointPolicy(allow_exact={allow}))
    return adapter.verify(target or _target(url), smoke or {"q": "ping"}, {"workspace_id": "workspace_a"})


def test_auth_denied_timeout_mapping_size_and_error_envelope():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path.endswith("/401"):
                self.send_response(401)
                self.end_headers()
                return
            if self.path.endswith("/slow"):
                time.sleep(1)
                self.send_response(200)
                self.end_headers()
                return
            if self.path.endswith("/big"):
                body = b'{"answer":"' + (b"x" * 2000) + b'"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.endswith("/err"):
                body = b'{"error":"remote failed"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.endswith("/redir"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/steal")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"other":1}')

    server, url, allow = _serve(Handler)
    try:
        base = url.rsplit("/", 1)[0]
        assert _invoke(base + "/401", allow).outcome == "auth_denied"
        assert _invoke(base + "/slow", allow, _target(base + "/slow", timeout_seconds=0.05)).outcome == "timeout"
        assert _invoke(base + "/invoke", allow, _target(url, output_mapping={"final_response": "/missing"})).outcome == "mapping_error"
        assert _invoke(base + "/big", allow).outcome == "oversized_response"
        assert _invoke(base + "/err", allow).outcome == "error_envelope"
        assert _invoke(base + "/redir", allow).outcome == "redirect_blocked"
    finally:
        server.shutdown()
        server.server_close()


def test_retries_are_rejected():
    adapter = HttpJsonAdapter(EndpointPolicy(allow_exact={"127.0.0.1:9"}))
    result = adapter.invoke({"target": _target("http://127.0.0.1:9/invoke"), "retries": 1}, {}, {})
    assert result.outcome == "retries_forbidden"


def test_retrieval_mapping_emits_authoritative_ranked_evidence():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"answer":"grounded","retrieval":{"query":"what?",'
                b'"chunks":["doc-b","doc-a"],"scores":[0.9,0.8],"source":"index-v1"}}'
            )

    server, url, allow = _serve(Handler)
    try:
        target = _target(
            url,
            retrieval_mapping={
                "query": "/retrieval/query",
                "chunks": "/retrieval/chunks",
                "scores": "/retrieval/scores",
                "source": "/retrieval/source",
            },
        )
        result = HttpJsonAdapter(EndpointPolicy(allow_exact={allow})).invoke(
            {"target": target}, {"q": "what?"},
            {"run_id": "run", "workspace_id": "ws", "case_id": "case",
             "attempt_id": "attempt", "repeat_index": 0, "attempt": 0},
        )
        assert result.outcome == "ok"
        assert result.capabilities["retrieval"] == "observed"
        assert len(result.trace_events) == 1
        event = result.trace_events[0]
        assert event.type is EventType.RETRIEVAL
        assert event.source is Source.ADAPTER
        assert event.payload == {
            "query": "what?", "chunks": ["doc-b", "doc-a"],
            "scores": [0.9, 0.8], "source": "index-v1",
        }
    finally:
        server.shutdown()
        server.server_close()
