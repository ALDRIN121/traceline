from llm_agent_eval.targets.sessions import SessionManager
from llm_agent_eval.targets.http_session import HttpSessionAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy


def test_http_session_enforces_scripted_approval_and_emits_turn_evidence():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            size = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}")
            calls.append((self.path, body))
            if self.path == "/init":
                self._json(200, {"session_id": "session-1", "state": "awaiting_input",
                                 "requested_input": "Approve refund?"})
            else:
                self._json(200, {"output": {"answer": "approved"}})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = HttpSessionAdapter(
            policy=EndpointPolicy(allow_exact={f"127.0.0.1:{server.server_port}"})
        ).invoke(
            {"target": {
                "init_url": f"{base}/init",
                "turn_url_template": f"{base}/sessions/{{session_id}}/turns",
            }},
            {"question": "request refund"},
            {
                "run_id": "run", "workspace_id": "ws", "case_id": "case", "attempt_id": "attempt",
                "interaction_script": [
                    {"kind": "wait_for_input", "requested_input": "Approve refund?"},
                    {"kind": "user_response", "provided_input": "yes"},
                ],
            },
        )
        assert result.outcome == "ok"
        assert calls[1][1] == {"input": "yes"}
        assert [event.type.value for event in result.trace_events] == [
            "wait_for_input", "user_response"
        ]
    finally:
        server.shutdown()
        server.server_close()


def test_http_session_closes_remote_session_when_script_is_rejected():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            body = {"session_id": "session-1", "state": "completed"} if self.path == "/init" else {}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = HttpSessionAdapter(
            policy=EndpointPolicy(allow_exact={f"127.0.0.1:{server.server_port}"})
        ).invoke(
            {"target": {
                "init_url": f"{base}/init",
                "turn_url_template": f"{base}/sessions/{{session_id}}/turns",
                "close_url_template": f"{base}/sessions/{{session_id}}/close",
            }},
            {"question": "request refund"},
            {
                "interaction_script": [{"kind": "wait_for_input", "requested_input": "Approve?"}],
            },
        )
        assert result.outcome == "approval_not_requested"
        assert "/sessions/session-1/close" in paths
    finally:
        server.shutdown()
        server.server_close()


def test_stateful_sessions_are_unique_per_case_repeat_and_cleaned_up():
    calls = []
    manager = SessionManager(
        initialize=lambda session_id: calls.append(("init", session_id)),
        close=lambda session_id: calls.append(("close", session_id)),
    )
    first = manager.start("case-1", 0)
    second = manager.start("case-1", 1)
    assert first.session_id != second.session_id
    manager.turn(first.session_id, {"message": "hello"}, lambda *_: {"ok": True})
    manager.reset(first.session_id)
    manager.close_session(first.session_id)
    assert any(kind == "close" for kind, _ in calls)
