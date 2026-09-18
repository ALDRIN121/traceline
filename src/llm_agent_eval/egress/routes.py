"""Installation-owned provider route configuration for the recording proxy.

The route file contains routing and pricing metadata only. Provider secrets
remain encrypted workspace references and are resolved by the trusted worker
at outbound time. Input token counting is deliberately a named trusted
implementation; arbitrary byte-length estimates are never accepted as a
metering authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..contracts import WorkflowError
from .recording import EgressDenied, ProviderRoute

__all__ = ["ProviderRouteRegistry"]

_MAX_CONFIG_BYTES = 1 * 1024 * 1024
_SUPPORTED_COUNTERS = frozenset({"trusted_chat_messages"})


class ProviderRouteRegistry:
    """Load and validate the install-owned provider route table.

    ``token_counter`` is injectable only for deterministic tests. Production
    uses the LiteLLM tokenizer and fails closed if the request shape cannot be
    counted. The registry never accepts a callable or credential from JSON.
    """

    def __init__(
        self,
        path: Path,
        *,
        token_counter: Callable[[ProviderRoute, Mapping[str, Any]], int] | None = None,
    ):
        self.path = Path(path)
        self._token_counter = token_counter or self._litellm_token_counter

    def load(self) -> list[ProviderRoute]:
        self._check_file()
        try:
            raw = json.loads(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise WorkflowError(
                "provider route configuration is invalid",
                code="proxy_routes_invalid", status=409,
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {"routes"} or not isinstance(raw["routes"], list):
            raise WorkflowError(
                "provider route configuration must contain a routes list",
                code="proxy_routes_invalid", status=409,
            )
        if not raw["routes"]:
            raise WorkflowError(
                "provider route configuration contains no routes",
                code="proxy_routes_invalid", status=409,
            )

        routes: list[ProviderRoute] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in raw["routes"]:
            route = self._route(item)
            key = (
                route.provider.lower(), route.host.lower(), route.path, route.model,
            )
            if key in seen:
                raise WorkflowError(
                    "duplicate provider route",
                    code="proxy_routes_invalid", status=409,
                )
            seen.add(key)
            routes.append(route)
        return routes

    def _check_file(self) -> None:
        if not self.path.is_file() or self.path.is_symlink():
            raise WorkflowError(
                "provider route configuration is unavailable",
                code="proxy_routes_unavailable", status=409,
            )
        try:
            mode = self.path.stat().st_mode
            size = self.path.stat().st_size
        except OSError as exc:
            raise WorkflowError(
                "provider route configuration is unavailable",
                code="proxy_routes_unavailable", status=409,
            ) from exc
        if mode & 0o022:
            raise WorkflowError(
                "provider route configuration is writable by another user",
                code="proxy_routes_unsafe", status=409,
            )
        if size > _MAX_CONFIG_BYTES:
            raise WorkflowError(
                "provider route configuration is too large",
                code="proxy_routes_invalid", status=409,
            )

    def _route(self, raw: Any) -> ProviderRoute:
        if not isinstance(raw, dict):
            raise WorkflowError(
                "provider route must be an object",
                code="proxy_routes_invalid", status=409,
            )
        allowed = {
            "host", "origin", "path", "provider", "model", "secret_ref",
            "dummy_key", "max_input_tokens", "max_output_tokens",
            "input_micros_per_token", "output_micros_per_token", "price_version",
            "max_request_bytes", "input_token_counter",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise WorkflowError(
                "unknown provider route field",
                code="proxy_routes_invalid", status=409,
            )
        counter_name = raw.get("input_token_counter")
        if counter_name not in _SUPPORTED_COUNTERS:
            raise WorkflowError(
                "provider route requires a trusted input token counter",
                code="proxy_routes_invalid", status=409,
            )
        required = (
            "host", "path", "model", "secret_ref", "dummy_key",
            "max_input_tokens", "max_output_tokens", "input_micros_per_token",
            "output_micros_per_token", "price_version",
        )
        if any(key not in raw for key in required):
            raise WorkflowError(
                "provider route is missing required metadata",
                code="proxy_routes_invalid", status=409,
            )
        try:
            holder: dict[str, ProviderRoute] = {}

            def bound(body: Mapping[str, Any]) -> int:
                value = self._token_counter(holder["route"], body)
                if type(value) is not int or value < 0:
                    raise EgressDenied("input_token_count_unavailable")
                return value

            route = ProviderRoute(
                host=raw["host"], path=raw["path"], model=raw["model"],
                origin=raw.get("origin"), provider=raw.get("provider", "openai"),
                secret_ref=raw["secret_ref"], dummy_key=raw["dummy_key"],
                max_input_tokens=raw["max_input_tokens"],
                max_output_tokens=raw["max_output_tokens"],
                input_micros_per_token=raw["input_micros_per_token"],
                output_micros_per_token=raw["output_micros_per_token"],
                price_version=raw["price_version"],
                max_request_bytes=raw.get("max_request_bytes", 262_144),
                input_token_bound=bound,
            )
            holder["route"] = route
            return route
        except WorkflowError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError(
                "provider route metadata is invalid",
                code="proxy_routes_invalid", status=409,
            ) from exc

    @staticmethod
    def _litellm_token_counter(route: ProviderRoute, body: Mapping[str, Any]) -> int:
        messages = body.get("messages") if isinstance(body, Mapping) else None
        if not isinstance(messages, list):
            raise EgressDenied("input_token_count_unavailable")
        try:
            from litellm import token_counter
            value = token_counter(model=route.model, messages=messages)
        except Exception as exc:
            raise EgressDenied("input_token_count_unavailable") from exc
        if type(value) is not int or value < 0:
            raise EgressDenied("input_token_count_unavailable")
        return value
