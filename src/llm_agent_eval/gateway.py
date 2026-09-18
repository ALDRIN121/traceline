"""The ModelGateway — the platform's own model surface (engine §12C).

The platform's model calls (harness authoring §31A, LLM judge §15A) go
through this interface; agent-under-test calls are never here — the proxy
observes and meters those (§12A). Two surfaces are never conflated.

§12C sharp edges honored here, stated so they are never re-litigated:

- **Sampling parameters are never set** (§11C.2): determinism never comes
  from ``temperature: 0`` — the schema is the stabilizer. No sampling
  parameter appears in any request this module composes.
- **Usage is read, never assumed** (§31B): ``GatewayResponse`` carries the
  provider-reported input/output token counts (0 only when the provider
  omits usage — honest, never an estimate). The harness's context-budget
  layer reads these fields.
- **``chat_json`` returns UNTRUSTED dicts** (STYLE invariant 2): the gateway
  requests JSON output but never validates the payload against the caller's
  schema — the caller always validates with pydantic. The gateway never
  validates.
- **The API key lives only in config** (``DEEPSEEK_API_KEY``, ModelConfig):
  never logged, never persisted, never in an error message. Provider error
  bodies are deliberately never surfaced — providers can echo the key inside
  authentication-error text.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import ModelConfig

# LiteLLM otherwise fetches its model-cost map remotely during import. The
# engine's pricing must be versioned and local, and this project never phones
# home from a self-hosted install.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

__all__ = [
    "GatewayError",
    "GatewayResponse",
    "ModelGateway",
    "DeepSeekGateway",
    "MockGateway",
    "LiteLLMGateway",
]

class GatewayError(Exception):
    """Normalized provider failure; never include provider response bodies."""

    def __init__(self, message: str, *, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


def _provider_error(exc: Exception) -> GatewayError:
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or name in {"Timeout", "APITimeoutError"}:
        code = "timeout"
    elif status in {401, 403} or name in {"AuthenticationError", "PermissionDeniedError"}:
        code = "auth_denied"
    elif status == 429 or name == "RateLimitError":
        code = "rate_limited"
    else:
        code = "provider_error"
    return GatewayError(f"provider request failed ({code})", code=code)


@dataclass(frozen=True)
class GatewayResponse:
    """One chat completion's payload. ``input_tokens``/``output_tokens`` are
    the provider-reported usage — read, never assumed (§31B.2/§31B.3)."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int


class ModelGateway(ABC):
    """The platform's own model surface (§12C.1): ``chat`` for plain
    completion, ``chat_json`` for JSON-mode output. ``provider``/``model``
    are the bound instrument identity (§15A.5), fixed per gateway — a judge
    bound to this gateway never falls back to another model (§12C.4)."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """The provider name (e.g. ``deepseek``) — part of the binding."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The exact model id + version the gateway is bound to."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema_hint: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        """A plain chat completion. No sampling parameters are ever set."""

    @abstractmethod
    def chat_json(
        self,
        messages: list[dict[str, Any]],
        json_schema_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request JSON output and return the parsed body as a dict. The dict
        is UNTRUSTED — the caller validates with pydantic; the gateway never
        validates. ``json_schema_hint`` is a prompt-side artifact (the caller
        embeds it in its messages); providers that support response-format
        JSON mode get it requested. Raises GatewayError when the response is
        not a JSON object."""

    def close(self) -> None:
        """Release transport resources (no-op default)."""


class DeepSeekGateway(ModelGateway):
    """DeepSeek via httpx against the OpenAI-compatible chat/completions
    endpoint (``{base_url}/chat/completions``), Bearer auth, ``model`` from
    the ModelConfig (default ``deepseek-v4-flash``). No streaming yet.

    The API key exists only in the ModelConfig (env) — this class never logs
    or persists it, and no error message ever contains it. Sampling
    parameters are never set (§11C.2). An optional injected ``client`` (e.g.
    httpx.MockTransport in tests) replaces the default; an injected client
    is not closed by :meth:`close`.
    """

    _DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

    def __init__(self, config: ModelConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    @property
    def provider(self) -> str:
        return self._config.provider

    @property
    def model(self) -> str:
        return self._config.model

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._DEFAULT_TIMEOUT)
            self._owns_client = True
        return self._client

    def _post(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool,
    ) -> tuple[str, str, int, int]:
        if not self._config.has_key:
            raise GatewayError(
                "no API key configured (DEEPSEEK_API_KEY) — the gateway is offline"
            )
        body: dict[str, Any] = {"model": self._config.model, "messages": messages}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._ensure_client().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise GatewayError(f"provider request failed ({exc.__class__.__name__})") from exc
        # Provider error bodies are deliberately never surfaced: providers can
        # echo the API key inside authentication-error text. Status code only.
        if response.status_code != 200:
            raise GatewayError(f"provider returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise GatewayError("provider returned a non-JSON response body") from exc
        try:
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError("provider response has no choices[0].message.content") from exc
        # Usage is read, never assumed (§31B): the provider-reported counts;
        # 0 only when the provider omits the usage block.
        usage = data.get("usage") or {}
        try:
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            input_tokens = output_tokens = 0
        model = data.get("model") or self._config.model
        return content, str(model), input_tokens, output_tokens

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema_hint: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        content, model, input_tokens, output_tokens = self._post(messages, json_mode=False)
        return GatewayResponse(content, model, input_tokens, output_tokens)

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        json_schema_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content, _, _, _ = self._post(messages, json_mode=True)
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise GatewayError("provider returned content that is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise GatewayError(
                f"provider returned a JSON {type(parsed).__name__}, expected an object"
            )
        return parsed

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


class MockGateway(ModelGateway):
    """Test double for the ModelGateway: canned responses from a callable
    registry.

    ``handlers`` maps a kind (``"chat"`` | ``"chat_json"``) to either a
    callable or a list of callables. A callable receives
    ``(messages, json_schema_hint)`` and returns the kind's payload: a
    :class:`GatewayResponse` for ``"chat"``, a dict for ``"chat_json"``. A
    *list* is consumed in order — the last callable repeats for subsequent
    calls — so a test can script "first an invalid spec, then a valid one".
    Every call is recorded on ``.calls`` as ``(kind, messages,
    json_schema_hint)``; an unregistered kind raises :class:`GatewayError`.
    """

    def __init__(
        self,
        handlers: dict[str, Callable | list[Callable]] | None = None,
        *,
        model: str = "mock-model",
        provider: str = "mock",
    ) -> None:
        self._handlers: dict[str, list[Callable]] = {}
        for kind, value in (handlers or {}).items():
            self._handlers[kind] = list(value) if isinstance(value, (list, tuple)) else [value]
        self._pos: dict[str, int] = {kind: 0 for kind in self._handlers}
        self._provider = provider
        self._model = model
        #: Every call, in order: (kind, messages, json_schema_hint).
        self.calls: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = []

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def _next(self, kind: str) -> Callable:
        queue = self._handlers.get(kind)
        if not queue:
            raise GatewayError(f"MockGateway has no {kind!r} handler registered")
        pos = min(self._pos[kind], len(queue) - 1)
        handler = queue[pos]
        if pos < len(queue) - 1:
            self._pos[kind] += 1
        return handler

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema_hint: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        self.calls.append(("chat", messages, json_schema_hint))
        result = self._next("chat")(messages, json_schema_hint)
        if not isinstance(result, GatewayResponse):
            raise TypeError(
                f"MockGateway chat handler must return GatewayResponse, "
                f"got {type(result).__name__}"
            )
        return result

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        json_schema_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("chat_json", messages, json_schema_hint))
        result = self._next("chat_json")(messages, json_schema_hint)
        if not isinstance(result, dict):
            raise TypeError(
                f"MockGateway chat_json handler must return a dict, "
                f"got {type(result).__name__}"
            )
        return result


class LiteLLMGateway(ModelGateway):
    """Platform calls through LiteLLM SDK, with explicit immutable profile policy.

    ModelConfig construction remains for development compatibility. Release
    services should use from_profile, which resolves only the pinned secret
    reference and never falls back to another model or retries silently.
    """

    def __init__(self, config: ModelConfig, *, completion=None, provider: str | None = None):
        self._config = config
        self._completion = completion
        self._provider = provider or config.provider
        self._profile = None

    @classmethod
    def from_profile(cls, profile, *, resolve_secret, completion=None):
        from .profiles import ModelProfile
        profile = ModelProfile.model_validate(profile.model_dump())
        secret = resolve_secret(profile.secret_ref) if profile.secret_ref else ""
        gateway = cls(ModelConfig(provider=profile.provider, model=profile.model,
                                  api_key=secret, base_url=profile.base_url or "",
                                  api_version=profile.api_version, allow_keyless=profile.keyless),
                      completion=completion)
        gateway._profile = profile
        return gateway

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._config.model

    def _complete(self, messages, *, json_mode: bool, json_schema_hint=None):
        if not self._config.can_authenticate:
            raise GatewayError("no API key configured — the gateway is offline", code="auth_denied")
        profile = self._profile
        if json_mode and profile and profile.structured_output == "none":
            raise GatewayError("profile does not support structured output", code="unsupported_capability")
        completion = self._completion
        if completion is None:
            import litellm
            litellm.telemetry = False
            completion = litellm.completion
        body: dict[str, Any] = {
            "model": profile.litellm_model if profile else self._config.model,
            "messages": messages,
            "timeout": profile.timeout_seconds if profile else 60,
            "num_retries": 0,
        }
        if self._config.has_key:
            body["api_key"] = self._config.api_key
        elif profile and profile.keyless:
            # Prevent provider SDKs from discovering a real ambient credential.
            body["api_key"] = "eval-engine-keyless"
        if self._config.base_url:
            body["api_base"] = self._config.base_url
        if self._config.api_version:
            body["api_version"] = self._config.api_version
        if profile:
            body.update(profile.parameters)
        if json_mode:
            schema_mode = json_schema_hint is not None and (not profile or profile.structured_output == "json_schema")
            body["response_format"] = (
                {"type": "json_schema", "json_schema": {"name": "structured_response", "schema": json_schema_hint, "strict": True}}
                if schema_mode else {"type": "json_object"}
            )
            if json_schema_hint and not schema_mode:
                body["messages"] = [*messages, {"role": "user", "content": "Return JSON matching this schema: " + json.dumps(json_schema_hint)}]
        try:
            return completion(**body)
        except GatewayError:
            raise
        except Exception as exc:
            raise _provider_error(exc) from None

    @staticmethod
    def _usage(response) -> tuple[str, str, int, int]:
        try:
            choice = response.choices[0]
            message = choice.message
            if getattr(message, "refusal", None) or getattr(choice, "finish_reason", None) == "content_filter":
                raise GatewayError("provider refused the request", code="refusal")
            content = getattr(message, "content", None)
            if not isinstance(content, str):
                raise ValueError("missing content")
            usage = getattr(response, "usage", None)
            counts = [getattr(usage, field, 0) or 0 for field in ("prompt_tokens", "completion_tokens")]
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError("invalid usage")
            return content, str(getattr(response, "model", None) or ""), counts[0], counts[1]
        except GatewayError:
            raise
        except (AttributeError, IndexError, TypeError, ValueError):
            raise GatewayError("provider response violates the completion contract", code="provider_error") from None

    def chat(self, messages, *, json_schema_hint=None) -> GatewayResponse:
        content, model, input_tokens, output_tokens = self._usage(self._complete(messages, json_mode=False))
        return GatewayResponse(content, model or self.model, input_tokens, output_tokens)

    def chat_json(self, messages, json_schema_hint=None) -> dict[str, Any]:
        # Schema is call-local: concurrent harness/judge calls cannot exchange it.
        content, _, _, _ = self._usage(self._complete(messages, json_mode=True, json_schema_hint=json_schema_hint))
        try:
            parsed = json.loads(content, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (TypeError, ValueError):
            raise GatewayError("provider returned content that is not valid JSON", code="malformed_json") from None
        if not isinstance(parsed, dict):
            raise GatewayError("provider returned JSON that is not an object", code="malformed_json")
        return parsed
