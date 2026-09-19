"""Tested OpenAI-compatible stateless JSON target adapter."""

from __future__ import annotations

import json
from typing import Any

from .http_json import HttpJsonAdapter


class OpenAICompatibleAdapter(HttpJsonAdapter):
    """Adapt ordinary case inputs to the OpenAI chat-completions envelope.

    This is deliberately limited to synchronous JSON responses. Streaming,
    tool-call, usage and provider-cost claims remain unavailable unless the
    target declares and verifies a separate supported capability.
    """

    @staticmethod
    def _messages(case_input: Any) -> dict[str, Any]:
        if isinstance(case_input, dict) and isinstance(case_input.get("messages"), list):
            return case_input
        encoded = json.dumps(case_input, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return {"messages": [{"role": "user", "content": encoded}]}

    def invoke(self, invocation_manifest, case_input, execution_context):
        target = dict(invocation_manifest["target"])
        if not target.get("request_mapping"):
            case_input = self._messages(case_input)
        if not target.get("output_mapping") or target.get("output_mapping") == {"final_response": "/answer"}:
            target["output_mapping"] = {"final_response": "/choices/0/message/content"}
        manifest = dict(invocation_manifest)
        manifest["target"] = target
        return super().invoke(manifest, case_input, execution_context)
