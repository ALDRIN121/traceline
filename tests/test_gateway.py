"""Tests for the ModelGateway surface (src/llm_agent_eval/gateway.py).

Offline: MockGateway round-trips (content/model/token fields), the callable
registry incl. scripted sequences (what the harness repair loop needs), and
the DeepSeekGateway request shape against httpx.MockTransport — URL, Bearer
auth, model from config, JSON mode, NO sampling parameters ever, usage read
from the response, and provider error bodies never surfaced (keys never
leak). The live provider is never touched here — test_gateway_live.py owns
that, marked `live` and skipped without a key.
"""

from __future__ import annotations

import json

import pytest
import httpx

from llm_agent_eval.config import ModelConfig
from llm_agent_eval.gateway import (
    DeepSeekGateway,
    GatewayError,
    GatewayResponse,
    MockGateway,
)


def echo_chat(messages, json_schema_hint=None):
    return GatewayResponse(content="pong", model="mock-model", input_tokens=7, output_tokens=3)


def echo_json(messages, json_schema_hint=None):
    return {"content": "echo", "model": "mock-model"}


# ---------------------------------------------------------------------------
# MockGateway — round-trip and registry
# ---------------------------------------------------------------------------


def test_mock_gateway_chat_round_trip():
    gateway = MockGateway({"chat": echo_chat})
    response = gateway.chat([{"role": "user", "content": "ping"}])
    assert response.content == "pong"
    assert response.model == "mock-model"
    assert response.input_tokens == 7
    assert response.output_tokens == 3


def test_mock_gateway_chat_json_returns_untrusted_dict():
    # chat_json returns the payload as-is — the gateway never validates: a
    # non-spec-shaped dict comes back untouched (the caller validates).
    gateway = MockGateway({"chat_json": lambda messages, hint: {"anything": True}})
    result = gateway.chat_json([{"role": "user", "content": "x"}])
    assert result == {"anything": True}


def test_mock_gateway_records_calls():
    gateway = MockGateway({"chat_json": echo_json})
    hint = {"type": "object"}
    gateway.chat_json([{"role": "user", "content": "x"}], hint)
    assert len(gateway.calls) == 1
    kind, messages, passed_hint = gateway.calls[0]
    assert kind == "chat_json"
    assert messages == [{"role": "user", "content": "x"}]
    assert passed_hint is hint


def test_mock_gateway_sequence_consumed_in_order_then_repeats():
    gateway = MockGateway(
        {
            "chat_json": [
                lambda m, h: {"draft": 1},
                lambda m, h: {"draft": 2},
                lambda m, h: {"draft": 3},
            ]
        }
    )
    assert gateway.chat_json([], None) == {"draft": 1}
    assert gateway.chat_json([], None) == {"draft": 2}
    assert gateway.chat_json([], None) == {"draft": 3}
    assert gateway.chat_json([], None) == {"draft": 3}  # last handler repeats


def test_mock_gateway_unregistered_kind_raises():
    gateway = MockGateway({"chat_json": echo_json})
    with pytest.raises(GatewayError, match="chat"):
        gateway.chat([])


def test_mock_gateway_chat_handler_must_return_response():
    gateway = MockGateway({"chat": echo_json})  # wrong payload type for chat
    with pytest.raises(TypeError, match="GatewayResponse"):
        gateway.chat([])


def test_mock_gateway_binding_identity():
    gateway = MockGateway(model="deepseek-v4-flash", provider="deepseek")
    assert gateway.provider == "deepseek"
    assert gateway.model == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# DeepSeekGateway — offline request shape (httpx.MockTransport)
# ---------------------------------------------------------------------------


def make_gateway(handler, *, api_key="sk-real-key-12345", **config_overrides):
    config = ModelConfig(api_key=api_key, **config_overrides)
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    gateway = DeepSeekGateway(config, client=client)
    return gateway, transport


def ok_response(json_body):
    return httpx.Response(200, json=json_body)


def test_deepseek_gateway_offline_without_key():
    gateway = DeepSeekGateway(ModelConfig(api_key=""))
    with pytest.raises(GatewayError, match="API key"):
        gateway.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(GatewayError, match="API key"):
        gateway.chat_json([{"role": "user", "content": "hi"}])


def test_deepseek_gateway_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return ok_response(
            {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            }
        )

    gateway, _ = make_gateway(handler)
    messages = [{"role": "user", "content": "ping"}]
    response = gateway.chat(messages)
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["auth"] == "Bearer sk-real-key-12345"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["messages"] == messages
    assert "temperature" not in captured["body"]  # §11C.2: never from sampling
    assert response.content == "hello"
    assert response.input_tokens == 12
    assert response.output_tokens == 5


def test_deepseek_gateway_json_mode_requested():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return ok_response(
            {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            }
        )

    gateway, _ = make_gateway(handler)
    result = gateway.chat_json([{"role": "user", "content": "json"}], {"type": "object"})
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert "temperature" not in captured["body"]
    assert result == {"ok": True}


def test_deepseek_gateway_usage_read_never_assumed():
    def handler(request: httpx.Request) -> httpx.Response:
        # Provider omits the usage block entirely — tokens read as 0, never
        # estimated.
        return ok_response(
            {"model": "deepseek-v4-flash", "choices": [{"message": {"content": "hi"}}]}
        )

    gateway, _ = make_gateway(handler)
    response = gateway.chat([{"role": "user", "content": "hi"}])
    assert response.content == "hi"
    assert response.input_tokens == 0
    assert response.output_tokens == 0


def test_deepseek_gateway_model_from_config():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return ok_response(
            {"model": "custom-model", "choices": [{"message": {"content": "hi"}}]}
        )

    gateway, _ = make_gateway(handler, model="custom-model", base_url="https://example.org/v1")
    response = gateway.chat([{"role": "user", "content": "hi"}])
    assert captured["body"]["model"] == "custom-model"
    assert response.model == "custom-model"  # echoed model wins, else config's


def test_deepseek_gateway_error_never_surfaces_provider_body():
    def handler(request: httpx.Request) -> httpx.Response:
        # A provider error body that echoes the key must never reach the
        # caller's error message.
        return httpx.Response(
            401,
            text='{"error": "Authentication Fails, Your api key: sk-real-key-12345 is invalid"}',
        )

    gateway, _ = make_gateway(handler)
    with pytest.raises(GatewayError) as excinfo:
        gateway.chat([{"role": "user", "content": "hi"}])
    assert "sk-real-key-12345" not in str(excinfo.value)
    assert "HTTP 401" in str(excinfo.value)


def test_deepseek_gateway_chat_json_non_object_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(
            {"model": "m", "choices": [{"message": {"content": "[1, 2, 3]"}}]}
        )

    gateway, _ = make_gateway(handler)
    with pytest.raises(GatewayError, match="object"):
        gateway.chat_json([{"role": "user", "content": "x"}])


def test_deepseek_gateway_malformed_json_content_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response(
            {"model": "m", "choices": [{"message": {"content": "not json"}}]}
        )

    gateway, _ = make_gateway(handler)
    with pytest.raises(GatewayError, match="valid JSON"):
        gateway.chat_json([{"role": "user", "content": "x"}])


def test_deepseek_gateway_missing_choices_shape_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response({"model": "m", "choices": []})

    gateway, _ = make_gateway(handler)
    with pytest.raises(GatewayError, match="choices"):
        gateway.chat([{"role": "user", "content": "x"}])


def test_deepseek_gateway_network_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    gateway, _ = make_gateway(handler)
    with pytest.raises(GatewayError, match="request failed"):
        gateway.chat([{"role": "user", "content": "x"}])
