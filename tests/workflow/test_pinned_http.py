"""Defensive transport contracts using synthetic responses and approved fixtures."""

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from llm_agent_eval.targets.http_json import HttpJsonAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy, PolicyDenied
from llm_agent_eval.targets.transport import PinnedTransport


def _pin(address="8.8.8.8"):
    return {"scheme": "https", "pinned_host": "agent.example", "pinned_port": 443,
            "addresses": [address]}


def test_transport_preserves_tls_hostname_and_host_header(monkeypatch):
    observed = []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request:
                        observed.append(request) or httpx.Response(200, json={}))
    with PinnedTransport(_pin()) as transport:
        transport.handle_request(httpx.Request("POST", "https://agent.example/invoke", json={}))
    assert observed[0].url.host == "8.8.8.8"
    assert observed[0].headers["host"] == "agent.example"
    assert observed[0].extensions["sni_hostname"] == "agent.example"


def test_transport_refuses_another_origin():
    with PinnedTransport(_pin()) as transport:
        with pytest.raises(httpx.TransportError):
            transport.handle_request(httpx.Request("GET", "https://other.example/"))


def test_approval_produces_addresses_even_for_exact_test_allowlist():
    policy = EndpointPolicy(allow_exact={"fixture.example:80"}, resolver=lambda *a, **k:
                            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    assert policy.authorize("http://fixture.example/")["addresses"] == ["127.0.0.1"]


@pytest.mark.parametrize("address", ["ff02::1", "fe80::1%en0", "::ffff:8.8.8.8", "::", "2001:db8::1"])
def test_ipv6_nonpublic_or_ambiguous_addresses_are_rejected(address):
    policy = EndpointPolicy(resolver=lambda *a, **k: [(None, None, None, None, (address, 443))])
    with pytest.raises(PolicyDenied):
        policy.authorize("https://agent.example/")


@pytest.mark.parametrize("url", ["https://agent.example:bad/", "https://[fe80::1%25en0]/", "https://agent.example:0/"])
def test_invalid_endpoint_is_a_policy_denial(url):
    with pytest.raises(PolicyDenied):
        EndpointPolicy().authorize(url)


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


def _invoke(monkeypatch, stream, *, status=200, headers=None, auth=None, url="https://agent.example/", limit=32):
    observed = []

    def handle(self, request):
        observed.append(request)
        return httpx.Response(status, headers=headers or {}, stream=stream)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    policy = EndpointPolicy(resolver=lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443))])
    result = HttpJsonAdapter(policy).invoke({"target": {
        "url": url, "auth": auth or {"type": "none"}, "max_response_bytes": limit,
    }}, {}, {"secret": "synthetic-secret"})
    return result, observed


def test_stream_limit_closes_response_without_reading_remainder(monkeypatch):
    stream = Stream([b"x" * 20, b"y" * 20, b"z" * 20])
    result, _ = _invoke(monkeypatch, stream)
    assert result.outcome == "oversized_response"
    assert stream.read_count == 2
    assert stream.closed


def test_redirect_chain_stops_at_first_response(monkeypatch):
    stream = Stream([b"unneeded body"])
    result, observed = _invoke(monkeypatch, stream, status=302,
                               headers={"location": "https://agent.example/next"})
    assert result.outcome == "redirect_blocked"
    assert len(observed) == 1
    assert stream.read_count == 0
    assert stream.closed


def test_authenticated_http_is_rejected_before_transport(monkeypatch):
    result, observed = _invoke(monkeypatch, Stream([]), auth={"type": "bearer"}, url="http://agent.example/")
    assert result.outcome == "https_required"
    assert observed == []


def test_compressed_response_is_rejected_before_decoding(monkeypatch):
    stream = Stream([b"untrusted compressed bytes"])
    result, _ = _invoke(monkeypatch, stream, headers={"content-encoding": "gzip"})
    assert result.outcome == "unsupported_content_encoding"
    assert stream.read_count == 0
    assert stream.closed


def test_ambient_proxy_settings_do_not_change_destination(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    result, observed = _invoke(monkeypatch, Stream([b'{"answer":"ok"}']))
    assert result.outcome == "ok"
    assert observed[0].url.host == "8.8.8.8"


def test_transport_never_resolves_hostname_after_approval(monkeypatch):
    resolved = []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request:
                        resolved.append(request.url.host) or httpx.Response(200, json={}))
    pin = EndpointPolicy(resolver=lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443))]).authorize("https://agent.example/")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("unexpected new DNS approval"))
    with PinnedTransport(pin) as transport:
        transport.handle_request(httpx.Request("GET", "https://agent.example/"))
    assert resolved == ["8.8.8.8"]


def test_real_connection_uses_approved_ip_with_dns_unavailable(monkeypatch):
    """An allowlisted local fixture exercises the actual socket transport."""
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hosts.append(self.headers["Host"])
            body = b'{"answer":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    original_resolver = socket.getaddrinfo
    dial_hosts = []

    def numeric_only(host, port, *args, **kwargs):
        dial_hosts.append(host)
        assert host == "127.0.0.1"
        return original_resolver(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    policy = EndpointPolicy(allow_exact={f"fixture.example:{port}"}, resolver=lambda *a, **k:
                            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    try:
        result = HttpJsonAdapter(policy).invoke({"target": {"url": f"http://fixture.example:{port}/"}}, {}, {})
        assert result.outcome == "ok"
        assert dial_hosts == ["127.0.0.1"]
        assert hosts == [f"fixture.example:{port}"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_public_ipv6_destination_retains_original_identity(monkeypatch):
    observed = []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request:
                        observed.append(request) or httpx.Response(200, json={}))
    with PinnedTransport(_pin("2606:4700:4700::1111")) as transport:
        transport.handle_request(httpx.Request("GET", "https://agent.example/"))
    assert observed[0].url.host == "2606:4700:4700::1111"
    assert observed[0].headers["host"] == "agent.example"
    assert observed[0].extensions["sni_hostname"] == "agent.example"
