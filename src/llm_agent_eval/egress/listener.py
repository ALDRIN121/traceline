"""Bounded per-run HTTP forward proxy for the recording egress."""

from __future__ import annotations

from dataclasses import dataclass
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socketserver
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .ca import InstallCA
from .recording import Budget, EgressDenied, ProviderRoute, RecordingEgress
from .transport import ProviderTransport


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _authority_hostname(authority: str) -> str:
    """Extract a DNS or IP-literal host from an HTTP CONNECT authority."""
    parsed = urlsplit(f"//{authority}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("invalid_connect_authority")
    return hostname


class _ProxyHandler(socketserver.StreamRequestHandler):
    server: "_ProxyServer"

    def handle(self):
        self.connection.settimeout(self.server.proxy.request_timeout)
        first = self.rfile.readline(16_384)
        if not first:
            return
        try:
            method, target, version = first.decode("ascii").strip().split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            self.server.proxy.respond(self.connection, 400, {"error": "invalid_request_line"})
            return
        if method.upper() == "CONNECT":
            self._handle_connect(target)
            return
        self._handle_http(self.connection, self.rfile, method, target, version)

    def _handle_connect(self, target: str):
        route = self.server.proxy.route_for_host(target)
        if route is None:
            self.server.proxy.respond(self.connection, 403, {"error": "route_not_allowed"})
            return
        if self.server.proxy.ca is None:
            self.server.proxy.respond(self.connection, 501, {"error": "tls_interception_unavailable"})
            return
        self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        hostname = _authority_hostname(target)
        try:
            tls = self.server.proxy.ca.context_for(hostname).wrap_socket(
                self.connection, server_side=True
            )
            tls.settimeout(self.server.proxy.request_timeout)
            stream = tls.makefile("rb")
            first = stream.readline(16_384)
            method, path, version = first.decode("ascii").strip().split(" ", 2)
            self._handle_http(tls, stream, method, path, version)
        except (OSError, UnicodeDecodeError, ValueError):
            return

    def _handle_http(self, connection, stream, method: str, target: str, version: str):
        try:
            headers = self._read_headers(stream)
            size = int(headers.get("content-length", "0"))
            if size < 0 or size > self.server.proxy.max_request_bytes:
                self.server.proxy.respond(connection, 413, {"error": "request_too_large"})
                return
            raw = stream.read(size)
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            parsed = urlsplit(target)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            host = headers.get("host") or parsed.netloc
            route = self.server.proxy.route_for_host(host)
            if route is None:
                self.server.proxy.respond(connection, 403, {"error": "route_not_allowed"})
                return
            if method.upper() != "POST":
                self.server.proxy.respond(connection, 405, {"error": "method_not_allowed"})
                return
            egress = self.server.proxy.egress_for(route)
            if body.get("stream") is True:
                status, payload = egress.forward_stream(path, headers, body)
                self.server.proxy.respond_stream(connection, status, payload)
                return
            status, payload = egress.forward(path, headers, body)
            self.server.proxy.respond(connection, status, payload)
        except EgressDenied as exc:
            self.server.proxy.respond(connection, 403, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError, OSError):
            self.server.proxy.respond(connection, 400, {"error": "invalid_request"})

    @staticmethod
    def _read_headers(stream):
        result = {}
        for _ in range(128):
            line = stream.readline(16_384)
            if not line or line in (b"\r\n", b"\n"):
                break
            key, value = line.decode("ascii").split(":", 1)
            result[key.strip().lower()] = value.strip()
        return result


class ProxyInstance:
    """One listener and one recording/budget context for one run."""

    def __init__(self, routes: list[ProviderRoute], budget: Budget,
                 secret_resolver: Callable[[str], str], record: Callable[[dict[str, Any]], None],
                 *, transport: Callable | None = None, ca: InstallCA | None = None,
                 host: str = "127.0.0.1", port: int = 0, request_timeout: float = 30.0):
        if not routes:
            raise ValueError("at least one provider route is required")
        self.routes = tuple(routes)
        self.budget = budget
        self._resolve = secret_resolver
        self._record = record
        self.transport = transport or ProviderTransport()
        self.ca = ca
        self.host, self.port, self.request_timeout = host, port, request_timeout
        self.max_request_bytes = max(route.max_request_bytes for route in routes)
        self._egress: dict[int, RecordingEgress] = {}
        self._server = None
        self._thread = None

    def route_for_host(self, host: str) -> ProviderRoute | None:
        host = host.lower().strip()
        return next((route for route in self.routes if route.host.lower() == host), None)

    def egress_for(self, route: ProviderRoute) -> RecordingEgress:
        key = id(route)
        if key not in self._egress:
            self._egress[key] = RecordingEgress(
                route, self.budget, self._resolve, self._record, send=self.transport,
            )
        return self._egress[key]

    def start(self) -> ProxyEndpoint:
        if self._server is not None:
            raise RuntimeError("proxy is already running")
        self._server = _ProxyServer((self.host, self.port), _ProxyHandler)
        self._server.proxy = self
        self._thread = self._server_thread()
        self._thread.start()
        return ProxyEndpoint(self.host, self._server.server_address[1])

    def _server_thread(self):
        import threading
        return threading.Thread(target=self._server.serve_forever, name="egress-proxy", daemon=True)

    def stop(self):
        if self._server is None:
            return {"state": "already_stopped"}
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None
        return {"state": "stopped"}

    @staticmethod
    def respond(connection, status: int, payload: Any):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed",
                  413: "Payload Too Large", 501: "Not Implemented"}.get(status, "Proxy Error")
        connection.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )

    @staticmethod
    def respond_stream(connection, status: int, chunks):
        reason = {200: "OK", 403: "Forbidden", 413: "Payload Too Large"}.get(status, "Proxy Error")
        try:
            connection.sendall(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n\r\n".encode()
            )
            for chunk in chunks:
                connection.sendall(chunk)
        except (EgressDenied, OSError):
            # Headers may already be visible to the sandbox. At that point a
            # failed/unmeterable stream is represented by connection close;
            # the authoritative proxy record has already closed the budget.
            return
