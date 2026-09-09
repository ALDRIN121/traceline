"""Local output-only HTTP agent used by hosted-target contract tests."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


class OutputOnlyAgent:
    def __init__(self):
        self.calls: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self):
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"health":"ok"}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                agent.calls.append({
                    "path": self.path,
                    "body": body.decode("utf-8"),
                    "authorization": self.headers.get("Authorization"),
                })
                payload = json.dumps({"answer": {"status": "ok"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/invoke"

    @property
    def allowlist_entry(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
