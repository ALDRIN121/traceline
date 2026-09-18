"""Provider outbound transport with no ambient proxy or credential discovery."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import httpx

from .cassettes import CassetteWorld, cassette_fingerprint


class ProviderTransport:
    def __init__(
        self, *, timeout: float = 30.0, max_response_bytes: int = 1_048_576,
        cassette: CassetteWorld | None = None, cassette_version: str = "v1",
        sender: Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, str]], tuple[int, Any]] | None = None,
    ):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.cassette = cassette
        self.cassette_version = cassette_version
        self.sender = sender

    def __call__(self, route: Mapping[str, Any], body: Mapping[str, Any], headers: Mapping[str, str]):
        def send():
            if self.sender is not None:
                return self.sender(route, body, headers)
            origin = route.get("origin") or f"https://{route['host']}"
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
