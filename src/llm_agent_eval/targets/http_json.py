"""R1 remote synchronous JSON HTTP adapter."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from ..contracts import CancellationResult, InvocationResult, VerificationRecord, WorkflowError
from .network_policy import EndpointPolicy, PolicyDenied
from .transport import PinnedTransport

GOLDEN_KEYS = frozenset({"expected", "gold", "label", "reference", "golden"})
UNSUPPORTED_MODES = frozenset({"streaming", "stateful", "async", "session"})
MAX_RESPONSE_BYTES = 1_048_576
FORBIDDEN_HEADERS = frozenset({
    "host", "content-length", "connection", "proxy-authorization", "proxy-connection",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
})


def apply_pointer(document: Any, pointer: str):
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise WorkflowError("JSON Pointer mappings must start with '/'")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise WorkflowError("Output mapping did not match the response", code="mapping_error") from exc
            if index < 0 or index >= len(current):
                raise WorkflowError("Output mapping did not match the response", code="mapping_error")
            current = current[index]
            continue
        if not isinstance(current, dict) or token not in current:
            raise WorkflowError("Output mapping did not match the response", code="mapping_error")
        current = current[token]
    return current


def reject_golden_mapping(mapping: dict) -> None:
    blob = str(mapping).lower()
    for key in GOLDEN_KEYS:
        if key in mapping or f"/{key}" in blob:
            raise WorkflowError("Expected labels cannot be mapped into agent-visible fields",
                                code="golden_mapping_forbidden")


def apply_request_mapping(case_input: Any, mapping: dict) -> dict:
    if not mapping:
        return case_input if isinstance(case_input, dict) else {}
    envelope = {"input": case_input} if not (isinstance(case_input, dict) and "input" in case_input) else case_input
    body: dict[str, Any] = {}
    for destination, source in mapping.items():
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise WorkflowError("Request mapping destinations must be JSON Pointers", code="mapping_error")
        value = apply_pointer(envelope, source) if isinstance(source, str) else source
        token = destination[1:].split("/", 1)[0]
        if token in GOLDEN_KEYS:
            raise WorkflowError("Expected labels cannot be mapped into agent-visible fields",
                                code="golden_mapping_forbidden")
        body[token] = value
    return body


class HttpJsonAdapter:
    def __init__(self, policy: EndpointPolicy):
        self.policy = policy

    def verify(self, target_version, smoke_input, execution_context) -> VerificationRecord:
        result = self.invoke({"target": target_version, "retries": 0}, smoke_input, execution_context)
        if result.outcome != "ok":
            return VerificationRecord(state=result.outcome, capabilities=result.capabilities,
                                      outcome=result.outcome, remote_uncertainty=result.remote_uncertainty)
        return VerificationRecord(
            state="verified",
            capabilities=result.capabilities,
            outcome="ok",
            observed_identity={
                "url": target_version.get("url"),
                "pinned_host": (target_version.get("pinned") or {}).get("pinned_host"),
            },
            remote_uncertainty=None,
        )

    def invoke(self, invocation_manifest, case_input, execution_context) -> InvocationResult:
        target = invocation_manifest["target"]
        capabilities = {
            "final_output": "unavailable",
            "tool_execution": "unavailable",
            "provider_cost": "unavailable",
        }
        if invocation_manifest.get("retries"):
            return InvocationResult(outcome="retries_forbidden", output=None, capabilities=capabilities)
        try:
            pin = self.policy.authorize(target["url"])
        except PolicyDenied as exc:
            return InvocationResult(outcome=exc.code, output=None, capabilities=capabilities,
                                    remote_uncertainty="blocked")
        stored = target.get("pinned") or {}
        if stored.get("addresses") and pin.get("addresses") and set(stored["addresses"]) != set(pin["addresses"]):
            return InvocationResult(outcome="dns_rebinding", output=None, capabilities=capabilities,
                                    remote_uncertainty="blocked")
        try:
            timeout = float(target.get("timeout_seconds", 5))
            limit = target.get("max_response_bytes", MAX_RESPONSE_BYTES)
            if not math.isfinite(timeout) or timeout <= 0 or type(limit) is not int or not 0 < limit <= MAX_RESPONSE_BYTES:
                raise ValueError("invalid HTTP limits")
        except (TypeError, ValueError):
            return InvocationResult(outcome="invalid_limits", output=None, capabilities=capabilities)
        mapping = target.get("request_mapping") or {}
        try:
            body = apply_request_mapping(case_input, mapping) if mapping else (
                case_input if isinstance(case_input, dict) else {}
            )
        except WorkflowError:
            return InvocationResult(outcome="mapping_error", output=None, capabilities=capabilities)
        # Identity encoding avoids an unbounded decompressor allocation before
        # the decoded-byte limit can be checked. Noncompliant servers fail closed.
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        auth = target.get("auth") or {"type": "none"}
        if auth.get("type", "none") != "none" and pin["scheme"] != "https":
            return InvocationResult(outcome="https_required", output=None, capabilities=capabilities)
        header_name = (auth.get("header") or "X-API-Key")
        if header_name.lower() in FORBIDDEN_HEADERS:
            return InvocationResult(outcome="auth_denied", output=None, capabilities=capabilities)
        if auth.get("type") == "bearer":
            headers["Authorization"] = f"Bearer {execution_context.get('secret') or ''}"
        elif auth.get("type") == "api_key":
            headers[header_name] = execution_context.get("secret") or ""
        try:
            with httpx.Client(transport=PinnedTransport(pin), follow_redirects=False,
                              timeout=timeout, verify=True, trust_env=False) as client:
                with client.stream(target.get("method") or "POST", target["url"], json=body, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        return InvocationResult(outcome="redirect_blocked", output=None, capabilities=capabilities,
                                                remote_uncertainty="redirect")
                    if response.status_code in {401, 403}:
                        return InvocationResult(outcome="auth_denied", output=None, capabilities=capabilities)
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        return InvocationResult(outcome="unsupported_content_encoding", output=None, capabilities=capabilities)
                    content = bytearray()
                    for chunk in response.iter_raw():
                        if len(chunk) > limit - len(content):
                            return InvocationResult(outcome="oversized_response", output=None, capabilities=capabilities)
                        content.extend(chunk)
        except httpx.TimeoutException:
            return InvocationResult(outcome="timeout", output=None, capabilities=capabilities,
                                    remote_uncertainty="timeout_after_possible_action")
        except httpx.HTTPError:
            return InvocationResult(outcome="transport_error", output=None, capabilities=capabilities,
                                    remote_uncertainty="transport")
        try:
            payload = json.loads(content)
        except ValueError:
            return InvocationResult(outcome="mapping_error", output=None, capabilities=capabilities)
        if response.status_code == 200 and isinstance(payload, dict) and payload.get("error"):
            return InvocationResult(outcome="error_envelope", output=payload, capabilities=capabilities)
        if response.status_code != 200:
            return InvocationResult(outcome="protocol_error", output=None, capabilities=capabilities)
        output_mapping = target.get("output_mapping") or {"final_response": ""}
        try:
            output = {name: apply_pointer(payload, pointer) for name, pointer in output_mapping.items()}
        except WorkflowError:
            return InvocationResult(outcome="mapping_error", output=None, capabilities=capabilities)
        capabilities = {**capabilities, "final_output": "observed"}
        return InvocationResult(
            outcome="ok", output=output, capabilities=capabilities,
            connector_observations={"status_code": response.status_code, "remote_cost": "unknown"},
            remote_cost="unknown",
        )

    def cancel(self, invocation_id, execution_context) -> CancellationResult:
        return CancellationResult(state="uncertain", invocation_id=invocation_id, observed=False)
