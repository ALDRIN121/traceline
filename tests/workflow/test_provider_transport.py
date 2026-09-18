from __future__ import annotations

from llm_agent_eval.egress.transport import ProviderTransport


class _Response:
    def model_dump(self):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class _Router:
    def __init__(self):
        self.calls = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return _Response()


def test_https_supported_provider_uses_embedded_litellm_router():
    router = _Router()
    seen = {}

    def factory(route, headers):
        seen["route"] = route
        seen["headers"] = headers
        return router

    transport = ProviderTransport(litellm_router_factory=factory)
    status, payload = transport(
        {
            "provider": "openai", "model": "gpt-4.1-mini",
            "origin": "https://api.example.test", "path": "/v1/chat/completions",
        },
        {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}]},
        {"Authorization": "Bearer real-provider-key"},
    )

    assert status == 200
    assert payload["usage"]["prompt_tokens"] == 2
    assert seen["headers"]["Authorization"] == "Bearer real-provider-key"
    assert router.calls[0]["model"] == "gpt-4.1-mini"
    assert router.calls[0]["stream"] is False


def test_generic_http_route_remains_explicit_fallback():
    calls = []
    transport = ProviderTransport(
        litellm_router_factory=lambda *_: (_ for _ in ()).throw(AssertionError("router used")),
        sender=lambda route, body, headers: (calls.append(route) or (200, {"ok": True})),
    )
    status, payload = transport(
        {"provider": "generic", "model": "fixture", "origin": "http://127.0.0.1:9", "path": "/invoke"},
        {"q": "hi"}, {},
    )
    assert (status, payload) == (200, {"ok": True})
    assert calls[0]["provider"] == "generic"
