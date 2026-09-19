from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx

from llm_agent_eval.egress.listener import ProxyInstance
from llm_agent_eval.egress.recording import Budget, ProviderRoute
from llm_agent_eval.egress.transport import ProviderTransport


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"received": body, "usage": {
            "prompt_tokens": 2, "completion_tokens": 3,
        }}).encode())

    def log_message(self, *_args):
        pass


def test_forward_proxy_records_and_brokers_dummy_credentials(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{upstream.server_port}"
    records = []
    route = ProviderRoute(
        host=f"127.0.0.1:{upstream.server_port}", origin=origin,
        path="/v1/chat/completions", model="fixture-model", secret_ref="provider",
        dummy_key="dummy-case-key", max_input_tokens=100, max_output_tokens=20,
        input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 2,
    )
    proxy = ProxyInstance(
        [route], Budget(1000), lambda ref: "real-provider-key", records.append,
        transport=ProviderTransport(),
    )
    endpoint = proxy.start()
    try:
        with httpx.Client(proxy=endpoint.url, trust_env=False) as client:
            response = client.post(
                f"{origin}/v1/chat/completions",
                headers={"Authorization": "Bearer dummy-case-key"},
                json={"model": "fixture-model", "max_tokens": 20, "messages": []},
            )
        assert response.status_code == 200
        assert response.json()["received"]["model"] == "fixture-model"
        assert records[-1]["cost_usd_micros"] == 13
        assert proxy.budget.spent == 13
    finally:
        proxy.stop()
        upstream.shutdown()


def test_proxy_denies_unknown_route_without_outbound_call(tmp_path):
    route = ProviderRoute(
        host="provider.example", origin="http://127.0.0.1:9", path="/v1/chat/completions",
        model="fixture-model", secret_ref="provider", dummy_key="dummy-case-key",
        max_input_tokens=100, max_output_tokens=20, input_micros_per_token=2,
        output_micros_per_token=3, price_version="fixture-v1",
        input_token_bound=lambda body: 2,
    )
    proxy = ProxyInstance([route], Budget(1000), lambda ref: "secret", lambda row: None)
    endpoint = proxy.start()
    try:
        with httpx.Client(proxy=endpoint.url, trust_env=False) as client:
            response = client.post(
                "http://provider.example/not-allowed",
                headers={"Authorization": "Bearer dummy-case-key"},
                json={"model": "fixture-model", "max_tokens": 1},
            )
        assert response.status_code == 403
    finally:
        proxy.stop()


def test_forward_proxy_streams_provider_sse_and_settles_usage():
    records = []
    route = ProviderRoute(
        host="provider.example", origin="https://provider.example",
        path="/v1/chat/completions", model="fixture-model", secret_ref="provider",
        dummy_key="dummy-case-key", max_input_tokens=100, max_output_tokens=20,
        input_micros_per_token=2, output_micros_per_token=3,
        price_version="fixture-v1", input_token_bound=lambda body: 2,
        allow_streaming=True,
    )
    transport = ProviderTransport(sender=lambda *_: (200, iter([
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
        b'data: [DONE]\n\n',
    ])))
    proxy = ProxyInstance(
        [route], Budget(1000), lambda ref: "real-provider-key", records.append,
        transport=transport,
    )
    endpoint = proxy.start()
    try:
        with httpx.Client(proxy=endpoint.url, trust_env=False) as client:
            response = client.post(
                "http://provider.example/v1/chat/completions",
                headers={"Authorization": "Bearer dummy-case-key"},
                json={"model": "fixture-model", "max_tokens": 20, "stream": True},
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert b"data: [DONE]" in response.content
        assert records[-1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}
        assert proxy.budget.spent == 7
    finally:
        proxy.stop()


def test_budget_allows_only_two_concurrent_worst_case_reservations():
    """The T14 acceptance: ten simultaneous 0.02 USD calls fit only twice."""
    budget = Budget(50_000)

    def reserve_once(_index):
        try:
            reservation = budget.reserve(20_000)
        except Exception as exc:  # public behavior is the typed denial
            return type(exc).__name__
        return reservation

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve_once, range(10)))

    assert results.count(20_000) == 2
    assert results.count("BudgetExceeded") == 8
