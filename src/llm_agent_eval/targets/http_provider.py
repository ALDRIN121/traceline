"""Bounded synchronous provider-envelope adapters for R2.

These adapters only normalize the request and response envelope. Transport,
endpoint pinning, authentication, response limits, and uncertainty handling
remain the shared ``HttpJsonAdapter`` contract.
"""

from __future__ import annotations

import json
from typing import Any

from ..contracts import InvocationResult
from .http_json import HttpJsonAdapter


_CAPABILITIES = {
    "final_output": "unavailable",
    "retrieval": "unavailable",
    "tool_execution": "unavailable",
    "provider_cost": "unavailable",
}


def _encoded_input(case_input: Any) -> str:
    return json.dumps(case_input, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class _ProviderEnvelopeAdapter(HttpJsonAdapter):
    default_output_pointer: str

    def _config_error(self, code: str) -> InvocationResult:
        return InvocationResult(
            outcome=code,
            output=None,
            capabilities=dict(_CAPABILITIES),
        )

    def _invoke_envelope(self, invocation_manifest, execution_context, body):
        target = dict(invocation_manifest["target"])
        model = target.get("model")
        max_tokens = target.get("max_tokens")
        if not isinstance(model, str) or not model:
            return self._config_error("provider_model_required")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1_000_000:
            return self._config_error("provider_max_tokens_invalid")
        target["request_mapping"] = {}
        if not target.get("output_mapping") or target.get("output_mapping") == {"final_response": "/answer"}:
            target["output_mapping"] = {"final_response": self.default_output_pointer}
        manifest = dict(invocation_manifest)
        manifest["target"] = target
        return super().invoke(manifest, body, execution_context)


class AnthropicMessagesAdapter(_ProviderEnvelopeAdapter):
    """Map ordinary case input to Anthropic's Messages JSON envelope."""

    default_output_pointer = "/content/0/text"

    def invoke(self, invocation_manifest, case_input, execution_context):
        target = invocation_manifest["target"]
        body = {
            "model": target.get("model"),
            "max_tokens": target.get("max_tokens"),
            "messages": [{"role": "user", "content": _encoded_input(case_input)}],
        }
        return self._invoke_envelope(invocation_manifest, execution_context, body)


class GoogleGenerativeAdapter(_ProviderEnvelopeAdapter):
    """Map ordinary case input to Google's Generative Language envelope."""

    default_output_pointer = "/candidates/0/content/parts/0/text"

    def invoke(self, invocation_manifest, case_input, execution_context):
        target = invocation_manifest["target"]
        body = {
            "contents": [{
                "role": "user",
                "parts": [{"text": _encoded_input(case_input)}],
            }],
            "generationConfig": {"maxOutputTokens": target.get("max_tokens")},
        }
        return self._invoke_envelope(invocation_manifest, execution_context, body)
