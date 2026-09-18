from __future__ import annotations

import json

import pytest

from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.egress.routes import ProviderRouteRegistry


def _route(**overrides):
    value = {
        "host": "api.example.test",
        "origin": "https://api.example.test",
        "path": "/v1/chat/completions",
        "provider": "openai",
        "model": "fixture-model",
        "secret_ref": "provider-secret",
        "dummy_key": "dummy-case-key",
        "max_input_tokens": 100,
        "max_output_tokens": 20,
        "input_micros_per_token": 2,
        "output_micros_per_token": 3,
        "price_version": "fixture-v1",
        "input_token_counter": "trusted_chat_messages",
    }
    value.update(overrides)
    return value


def test_route_registry_loads_provider_route_without_secret_values(tmp_path):
    path = tmp_path / "proxy-routes.json"
    path.write_text(json.dumps({"routes": [_route()]}), encoding="utf-8")

    registry = ProviderRouteRegistry(
        path, token_counter=lambda route, body: 7,
    )
    routes = registry.load()

    assert len(routes) == 1
    assert routes[0].secret_ref == "provider-secret"
    assert routes[0].input_token_bound({"messages": []}) == 7


def test_route_registry_rejects_duplicate_routes(tmp_path):
    path = tmp_path / "proxy-routes.json"
    path.write_text(json.dumps({"routes": [_route(), _route()]}), encoding="utf-8")

    with pytest.raises(WorkflowError, match="duplicate provider route"):
        ProviderRouteRegistry(path, token_counter=lambda route, body: 1).load()


def test_route_registry_fails_closed_for_missing_or_untrusted_counter(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(WorkflowError, match="provider route configuration is unavailable"):
        ProviderRouteRegistry(missing).load()

    path = tmp_path / "proxy-routes.json"
    path.write_text(json.dumps({"routes": [_route(input_token_counter="body_bytes")]}), encoding="utf-8")
    with pytest.raises(WorkflowError, match="input token counter"):
        ProviderRouteRegistry(path).load()


def test_route_registry_rejects_embedded_secret_fields(tmp_path):
    path = tmp_path / "proxy-routes.json"
    path.write_text(json.dumps({"routes": [_route(api_key="sk-live-should-not-be-here")]}), encoding="utf-8")

    with pytest.raises(WorkflowError, match="unknown provider route field"):
        ProviderRouteRegistry(path).load()
