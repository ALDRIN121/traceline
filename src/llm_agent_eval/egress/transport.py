"""Provider outbound transport with no ambient proxy or credential discovery."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .cassettes import CassetteWorld, cassette_fingerprint


class ProviderTransport:
    def __init__(
        self, *, timeout: float = 30.0, max_response_bytes: int = 1_048_576,
        cassette: CassetteWorld | None = None, cassette_version: str = "v1",
        sender: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, str]], tuple[int, Any]] | None = None,
        litellm_router_factory: Callable[[Mapping[str, Any], Mapping[str, str]], Any] | None = None,
    ):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.cassette = cassette
        self.cassette_version = cassette_version
        self.sender = sender
        self.litellm_router_factory = litellm_router_factory or _default_litellm_router_factory

    def __call__(self, route: Mapping[str, Any], body: Mapping[str, Any], headers: Mapping[str, str]):
        def send():
            if self.sender is not None:
                return self.sender(route, body, headers)
            origin = route.get("origin") or f"https://{route['host']}"
            provider = str(route.get("provider") or "generic").lower()
            if urlsplit(str(origin)).scheme == "https" and provider in {"openai", "anthropic", "google"}:
                router = self.litellm_router_factory(route, headers)
                request = dict(body)
                request["model"] = route["model"]
                request["stream"] = False
                response = router.completion(**request)
                payload = response.model_dump() if hasattr(response, "model_dump") else response
                if not isinstance(payload, Mapping):
                    raise ValueError("invalid_litellm_response")
                encoded = json.dumps(payload, allow_nan=False, ensure_ascii=True).encode("utf-8")
                if len(encoded) > self.max_response_bytes:
                    raise ValueError("response_too_large")
                status = getattr(response, "status_code", 200)
                return (status if type(status) is int else 200), dict(payload)
            url = origin.rstrip("/") + route["path"]
            outbound = {
                key: value for key, value in headers.items()
                if key.lower() not in {"host", "proxy-authorization", "proxy-connection"}
            }
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(url, json=body, headers=outbound)
                if len(response.content) > self.max_response_bytes:
                    raise ValueError("response_too_large")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ValueError("invalid_json_response") from exc
            return response.status_code, payload

        if self.cassette is None:
            return send()
        provider = str(route.get("provider") or "generic")
        path = str(route.get("path") or "")
        fingerprint = cassette_fingerprint(provider, path, body, self.cassette_version)
        return self.cassette.exchange(fingerprint, {"body": body}, send)


def _default_litellm_router_factory(
    route: Mapping[str, Any], headers: Mapping[str, str],
):
    """Build the embedded LiteLLM Router for one trusted HTTPS route.

    The real key arrives only from the recording proxy's trusted secret
    resolver. It is passed into the in-process Router configuration and never
    becomes part of the request body or cassette metadata.
    """
    import litellm

    provider = str(route.get("provider") or "openai").lower()
    model = str(route["model"])
    prefix = {"google": "gemini"}.get(provider, provider)
    litellm_model = model if "/" in model else f"{prefix}/{model}"
    auth_names = {
        "openai": "authorization", "anthropic": "x-api-key", "google": "x-goog-api-key",
    }
    auth_name = auth_names.get(provider, "authorization")
    credential = next(
        (value for key, value in headers.items() if str(key).lower() == auth_name),
        None,
    )
    if not isinstance(credential, str) or not credential:
        raise ValueError("provider_credential_missing")
    if auth_name == "authorization" and credential.lower().startswith("bearer "):
        credential = credential[7:]
    origin = str(route.get("origin") or f"https://{route['host']}")
    litellm_params = {
        "model": litellm_model,
        "api_key": credential,
        "api_base": origin,
    }
    return litellm.Router(
        model_list=[{"model_name": model, "litellm_params": litellm_params}],
        num_retries=0,
    )
