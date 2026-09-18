"""Offline contracts for administrator-managed model profiles."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.gateway import GatewayError, LiteLLMGateway
from llm_agent_eval.profiles import ModelProfile, ProfileStore


def profile(provider="openai", **kwargs):
    fields = {"name": "test", "provider": provider, "model": "pinned-model", "secret_ref": "secret-id"}
    if provider in {"ollama", "openai_compatible"}:
        fields["keyless"] = True
        fields["secret_ref"] = ""
        fields.setdefault("base_url", "http://127.0.0.1:11434")
    fields.update(kwargs)
    return ModelProfile(**fields)


def response(content='{"ok": true}', **message):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, **message))],
                           usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4), model="pinned-model")


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "deepseek", "azure", "ollama", "openai_compatible"])
def test_supported_profiles(provider):
    extras = {"api_version": "2025-01-01-preview", "base_url": "https://azure.example.test"} if provider == "azure" else {}
    if provider in {"ollama", "openai_compatible"}:
        extras["base_url"] = "http://127.0.0.1:11434"
    assert profile(provider, **extras).provider == provider


def test_profile_rejects_inline_secrets_and_remote_cleartext_auth():
    with pytest.raises(ValidationError):
        ModelProfile(name="bad", provider="openai", model="test", api_key="never-store")
    with pytest.raises(ValidationError):
        profile(base_url="http://remote.example.test")


def test_parameters_are_explicit_allowlist_not_litellm_drop_params():
    with pytest.raises(ValidationError):
        profile(parameters={"temperature": 0})
    with pytest.raises(ValidationError):
        profile(parameters={"max_tokens": 10}, supported_parameters=[])


def test_profile_requires_admin_before_storage():
    with pytest.raises(WorkflowError) as exc:
        ProfileStore(None).create("project", profile(), 0, Actor("editor", "workspace", "editor"))
    assert exc.value.code == "forbidden"


def test_profile_gateway_routes_and_sends_supported_schema():
    calls = []
    model = profile("anthropic", structured_output="json_schema", supported_parameters=["max_tokens"], parameters={"max_tokens": 40})
    gateway = LiteLLMGateway.from_profile(model, resolve_secret=lambda ref: "dummy-provider-key", completion=lambda **kw: calls.append(kw) or response())
    assert gateway.chat_json([], {"type": "object"}) == {"ok": True}
    call = calls[0]
    assert call["model"] == "anthropic/pinned-model"
    assert call["api_key"] == "dummy-provider-key"
    assert call["max_tokens"] == 40
    assert call["timeout"] == model.timeout_seconds
    assert call["num_retries"] == 0
    assert call["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert "temperature" not in call


def test_unsupported_structured_output_fails_before_provider():
    gateway = LiteLLMGateway.from_profile(profile(structured_output="none"), resolve_secret=lambda ref: "dummy", completion=lambda **kw: pytest.fail("must not call"))
    with pytest.raises(GatewayError) as exc:
        gateway.chat_json([])
    assert exc.value.code == "unsupported_capability"


@pytest.mark.parametrize("failure,code", [(TimeoutError(), "timeout"), (type("AuthenticationError", (Exception,), {})(), "auth_denied"), (RuntimeError("key-must-not-escape"), "provider_error")])
def test_gateway_failures_are_typed_and_secret_safe(failure, code):
    def fail(**kwargs):
        raise failure
    gateway = LiteLLMGateway.from_profile(profile(), resolve_secret=lambda ref: "dummy", completion=fail)
    with pytest.raises(GatewayError) as exc:
        gateway.chat_json([])
    assert exc.value.code == code
    assert "key-must-not-escape" not in str(exc.value)


@pytest.mark.parametrize("reply,code", [(response("no json"), "malformed_json"), (response(refusal="I refuse"), "refusal"), (SimpleNamespace(choices=[]), "provider_error")])
def test_malformed_and_refusal_failures(reply, code):
    gateway = LiteLLMGateway.from_profile(profile(), resolve_secret=lambda ref: "dummy", completion=lambda **kw: reply)
    with pytest.raises(GatewayError) as exc:
        gateway.chat_json([])
    assert exc.value.code == code
