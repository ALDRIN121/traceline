"""Forwarding, metering, and redaction rules for the recording egress proxy.

Foundations only: a caller supplies an outbound ``send`` callable. The
container topology that makes this the sandbox's sole route is owned by the
runtime agent and is not implemented here; until that integration exists this
module makes no release claim.

Design (locked decisions 1–2, invariants):

- Only explicitly configured provider routes are forwardable. Anything else —
  wrong path, wrong model, streaming, unbounded ``max_tokens`` — is denied
  before any credential is resolved.
- The sandbox holds a dummy key; real credentials are substituted only inside
  the outbound leg via a trusted resolver, and never recorded.
- Capture-time redaction: free-text request bodies and headers are never
  persisted. Only bounded, structured metadata (sizes, model, status, usage)
  is recorded, so PII can never reach storage through this path.
- Usage is read from the provider response, never assumed. Unknown usage or
  pricing for any observed request fails closed and closes the run budget —
  an unmetered egress path is worse than a stopped run.
- Adapter-generated events are first-class evidence but can never carry cost:
  cost is observed only at the proxy, on the wire.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from ..redaction import redact

# Adapter evidence is free text; a bounded PII sweep removes obvious email
# addresses before persistence, in addition to the shared secret/token rules.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

class EgressDenied(Exception):
    """Traffic is refused before it leaves the platform."""


class BudgetExceeded(EgressDenied):
    """The run's metered budget no longer permits another request."""


def _micros(value: int) -> str:
    return f"{value:,} micros"


class Budget:
    """Worst-case reservation + observed-spend reconciliation for one run.

    ``reserve`` debits the declared worst case (route maxima) before the
    request leaves. ``commit`` replaces the reservation with the provider's
    observed usage. ``uncertain`` (unmeterable traffic) closes the budget:
    nothing further may be forwarded under an unmeasurable run.
    """

    def __init__(self, budget_usd_micros: int):
        if not isinstance(budget_usd_micros, int) or budget_usd_micros < 0:
            raise ValueError("budget_usd_micros must be a nonnegative integer")
        self.total = budget_usd_micros
        self.spent = 0
        self._reserved = 0

    def reserve(self, worst_case_usd_micros: int) -> int:
        if self._reserved < 0:  # budget closed by an earlier unmeterable event
            raise BudgetExceeded("budget unavailable: unmeterable traffic observed")
        if worst_case_usd_micros > self.total - self.spent - self._reserved:
            raise BudgetExceeded(
                f"budget exceeded: {_micros(worst_case_usd_micros)} needed, "
                f"{_micros(self.total - self.spent - self._reserved)} remain"
            )
        self._reserved += worst_case_usd_micros
        return worst_case_usd_micros

    def commit(self, reservation: int, observed_usd_micros: int) -> None:
        self._reserved = max(0, self._reserved - reservation)
        self.spent += observed_usd_micros

    def uncertain(self, reservation: int | None = None) -> None:
        """Close the budget: observed usage was unknown, so the run is unmeterable."""
        self._reserved = -1
        if reservation is not None:
            self.spent += reservation
            self._reserved = 0 if self._reserved != -1 else -1


class ProviderRoute:
    """One administrator-declared provider endpoint the proxy may forward.

    ``input_token_bound`` is trusted engine code, not sandbox input. It must
    bound *all billable input* for this exact model/protocol, including message
    framing, tools and multimodal content, or raise for unsupported requests.
    No byte heuristic is a general token bound. Without a provider-specific
    counter the route refuses forwarding. The outbound adapter must enforce
    max_tokens as the total billable output bound (one completion, no hidden
    extra billed tokens); unsupported protocols must not install a counter.
    """

    def __init__(
        self,
        *,
        host: str,
        path: str,
        model: str,
        secret_ref: str,
        dummy_key: str,
        max_input_tokens: int,
        max_output_tokens: int,
        input_micros_per_token: int,
        output_micros_per_token: int,
        price_version: str,
        max_request_bytes: int = 262_144,
        input_token_bound: Callable[[Mapping[str, Any]], int] | None = None,
    ):
        if not host or not path.startswith("/"):
            raise ValueError("route requires an explicit host and absolute path")
        if input_token_bound is not None and not callable(input_token_bound):
            raise ValueError("input_token_bound must be a trusted callable")
        for value in (max_input_tokens, max_output_tokens, max_request_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("route limits must be positive integers")
        for value in (input_micros_per_token, output_micros_per_token):
            if type(value) is not int or value < 0:
                raise ValueError("token prices must be nonnegative integers")
        self.input_token_bound = input_token_bound
        self.host, self.path, self.model = host, path, model
        self.secret_ref, self.dummy_key = secret_ref, dummy_key
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.input_micros_per_token = input_micros_per_token
        self.output_micros_per_token = output_micros_per_token
        self.price_version = price_version
        self.max_request_bytes = max_request_bytes

    def authorize(self, path: str, body: Mapping[str, Any]) -> dict[str, int]:
        if path != self.path:
            raise EgressDenied("path_not_allowed")
        if body.get("model") != self.model:
            raise EgressDenied("model_not_allowed")
        if body.get("stream") is True:
            raise EgressDenied("streaming_not_supported")
        declared = body.get("max_tokens")
        if type(declared) is not int or declared <= 0:
            raise EgressDenied("unbounded_max_tokens")
        if declared > self.max_output_tokens:
            raise EgressDenied("max_tokens_exceeds_route_limit")
        try:
            encoded = json.dumps(body, allow_nan=False, ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise EgressDenied("invalid_request") from exc
        if len(encoded) > self.max_request_bytes:
            raise EgressDenied("request_too_large")
        if self.input_token_bound is None:
            raise EgressDenied("input_token_bound_unavailable")
        try:
            input_tokens = self.input_token_bound(body)
        except Exception as exc:
            raise EgressDenied("input_token_bound_unavailable") from exc
        if type(input_tokens) is not int or input_tokens < 0:
            raise EgressDenied("input_token_bound_unavailable")
        if input_tokens > self.max_input_tokens:
            raise EgressDenied("input_tokens_exceeds_route_limit")
        return {"input_tokens": input_tokens, "output_tokens": declared}

    def worst_case(self, input_tokens: int) -> int:
        return (
            input_tokens * self.input_micros_per_token
            + self.max_output_tokens * self.output_micros_per_token
        )

    def observed(self, usage: Mapping[str, Any] | None) -> tuple[int, dict[str, int]]:
        if not isinstance(usage, Mapping):
            raise EgressDenied("usage_unavailable")
        try:
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
        except (KeyError, TypeError) as exc:
            raise EgressDenied("usage_unavailable") from exc
        # Usage is authoritative provider accounting: booleans, floats or any
        # non-integer claim is not meterable evidence, not a coercion candidate.
        if type(input_tokens) is not int or type(output_tokens) is not int:
            raise EgressDenied("usage_unavailable")
        if input_tokens < 0 or output_tokens < 0:
            raise EgressDenied("usage_unavailable")
        micros = (
            input_tokens * self.input_micros_per_token
            + output_tokens * self.output_micros_per_token
        )
        return micros, {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        }


class RecordingEgress:
    """Validate, meter, substitute credentials, forward, record metadata."""

    def __init__(
        self,
        route: ProviderRoute,
        budget: Budget,
        secret_resolver: Callable[[str], str],
        record: Callable[[dict[str, Any]], None],
        *,
        send: Callable[[dict[str, Any], Mapping[str, Any], Mapping[str, str]], tuple[int, Any]] | None = None,
    ):
        self.route = route
        self.budget = budget
        self._resolve = secret_resolver
        self._record = record
        self._send = send or self._deny_without_transport

    @staticmethod
    def _deny_without_send(*args: Any) -> tuple[int, Any]:
        raise EgressDenied("no_outbound_transport")

    def _deny_without_transport(self, *args: Any) -> tuple[int, Any]:
        return self._deny_without_send(*args)

    def forward(
        self,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> tuple[int, Any]:
        limits = self.route.authorize(path, body)
        if not isinstance(headers.get("Authorization"), str) or (
            headers["Authorization"] != f"Bearer {self.route.dummy_key}"
        ):
            raise EgressDenied("credential_not_recognized")
        # Only the trusted provider-specific upper bound permits reservation.
        worst = self.route.worst_case(limits["input_tokens"])
        reservation = self.budget.reserve(worst)
        try:
            real = self._resolve(self.route.secret_ref)
        except Exception as exc:
            self.budget.uncertain(reservation)
            raise EgressDenied("secret_resolution_failed") from exc
        if not real or not isinstance(real, str):
            self.budget.uncertain(reservation)
            raise EgressDenied("secret_resolution_failed")
        outbound_headers = {**headers, "Authorization": f"Bearer {real}"}
        try:
            status, payload = self._send(
                {"host": self.route.host, "path": path, "model": self.route.model},
                body,
                outbound_headers,
            )
        except Exception as exc:
            # Any failure out of the outbound leg — including its own denials —
            # leaves upstream billing unknowable: treat the reservation as
            # spent and close the budget rather than continue unaccounted.
            self.budget.uncertain(reservation)
            self._record(self._metadata(status=None, usage=None, model=self.route.model))
            raise EgressDenied("upstream_unreachable") from exc
        try:
            response_usage = payload.get("usage") if isinstance(payload, Mapping) else None
            micros, usage = self.route.observed(response_usage)
        except EgressDenied as exc:
            # Unmeterable observed traffic closes the budget; the reservation
            # becomes spent so the run cannot continue unaccounted.
            self.budget.uncertain(reservation)
            self._record(self._metadata(status=status, usage=None, model=self.route.model))
            raise EgressDenied("usage_unavailable") from exc
        self.budget.commit(reservation, micros)
        bound_violated = (usage["prompt_tokens"] > limits["input_tokens"]
                          or usage["completion_tokens"] > limits["output_tokens"])
        if bound_violated:
            # Billing already happened: preserve observed cost but prohibit
            # any further request under the now-disproven bound.
            self.budget.uncertain()
        self._record(
            {
                **self._metadata(status=status, usage=usage, model=self.route.model),
                "cost_usd_micros": micros,
                "price_version": self.route.price_version,
            }
        )
        if bound_violated:
            raise EgressDenied("usage_exceeds_reserved_bound")
        return status, payload

    def record_adapter(self, event: Mapping[str, Any]) -> None:
        """Persist an adapter-generated event as independent evidence.

        Adapter events keep their own ``source`` and are never merged into
        proxy traffic; a proxy source label on adapter input is corrected to
        ``adapter`` so wire evidence cannot be forged from the sandbox. Cost
        is always None here: only the proxy observes provider cost.
        """
        clean = redact(dict(event), max_string_bytes=4096)
        payload = clean.content if isinstance(clean.content, Mapping) else {}

        def sweep(value: Any) -> tuple[Any, bool]:
            """Recursively remove email-shaped PII from string leaves."""
            if isinstance(value, str):
                if _EMAIL.search(value):
                    return _EMAIL.sub("[REDACTED-PII]", value), True
                return value, False
            if isinstance(value, Mapping):
                swept, found = {}, False
                for key, item in value.items():
                    clean_key, key_hit = sweep(key)
                    clean_item, item_hit = sweep(item)
                    swept[clean_key] = clean_item
                    found = found or key_hit or item_hit
                return swept, found
            if isinstance(value, (list, tuple)):
                items, found = [], False
                for item in value:
                    clean_item, item_hit = sweep(item)
                    swept_found = item_hit or found
                    found = swept_found
                    items.append(clean_item)
                return items, found
            return value, False

        payload, pii_found = sweep(payload)
        rules = tuple(sorted({*clean.detector_flags, "pii"})) if pii_found else clean.detector_flags
        if isinstance(payload, Mapping) and payload.get("source") == "proxy":
            payload = {**payload, "source": "adapter"}
        record = {
            "source": "adapter",
            "type": payload.get("type") if isinstance(payload, Mapping) else None,
            "redaction_state": {
                "status": (
                    "truncated" if clean.truncated
                    else "redacted" if rules
                    else "clean"
                ),
                "rules": list(rules),
            },
            "payload": payload,
            "cost_usd_micros": None,
        }
        self._record(record)

    def _metadata(
        self,
        *,
        status: int | None,
        usage: dict[str, int] | None,
        model: str,
    ) -> dict[str, Any]:
        return {
            "source": "proxy",
            "host": self.route.host,
            "path": self.route.path,
            "model": model,
            "status": status,
            "usage": usage,
            "cost_usd_micros": None,
            "price_version": self.route.price_version,
        }
