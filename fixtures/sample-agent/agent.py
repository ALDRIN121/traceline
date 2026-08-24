#!/usr/bin/env python3
"""Sample agent fixture — a small, well-formed support-triage agent.

The healthy evaluation target the engine's smoke gate and runs execute against
(the reference agent at ``fixtures/reference-agent/`` is the deliberately-
broken one; this fixture is well-formed on purpose).

Flow per case::

    read LLM_AGENT_EVAL_INPUT/case.json            # §11A file protocol
    delegation("support_triage")                   # TraceCapture
    llm_call -> llm_response                       # classify the request
    if refund: delegation("refund_specialist")
    tool_call search_articles -> tool_result       # TOOLS registered at import
    tool_call check_refund_eligibility -> result   # refund requests only
    llm_call -> llm_response                       # compose the final answer
    write LLM_AGENT_EVAL_OUTPUT/result.json        # atomic: tmp -> fsync -> rename

The LLM client is env-swappable — the same code runs:

  (a) offline against a mock OpenAI-compatible server: set
      ``LLM_AGENT_EVAL_LLM_BASE_URL`` and ``LLM_AGENT_EVAL_LLM_KEY`` (the
      tests point the agent at ``tests/fixtures/mock_llm_server.py``);
  (b) live against DeepSeek: the same two variables with a real key —
      exercised only by the coordinator, never by tests;
  (c) standalone with no env at all: capture no-ops (TraceCapture is a no-op
      sink without ``LLM_AGENT_EVAL_TRACE``) and the LLM falls back to a
      built-in deterministic classifier — no network, no key, no .env read.

Every LLM call, tool call/result, and delegation decision flows through
``TraceCapture``, so the engine's trace rules can observe the whole run. Cost
flows too: the client parses usage from the chat-completions response and the
agent passes a ``CostBlock`` through capture (with a deliberately wrong
``price_version`` — engine ingestion stamps the config's version, §12A.5, so
the stamping is observable in cost summaries).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

try:
    from llm_agent_eval.capture import TraceCapture
    from llm_agent_eval.events import CostBlock, TokenUsage
except ImportError:  # the repo may not be pip-installed — fall back to the src tree
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from llm_agent_eval.capture import TraceCapture
    from llm_agent_eval.events import CostBlock, TokenUsage

#: Module-level tool registry, populated at import time (static analysis can
#: see the tools — the reference agent's runtime dict is the broken pattern).
TOOLS: dict[str, object] = {}

#: Module-global, process-local state — the fresh-process-per-case probe: when
#: LLM_AGENT_EVAL_MUTATE=1 the agent appends here and reports the length; each
#: attempt must observe a fresh list (§11A invariant, engine).
_STATE: list[str] = []

#: Live-mode default model (the platform's ModelConfig default, config.py).
#: Live mode is opt-in: a base URL must be set explicitly via
#: LLM_AGENT_EVAL_LLM_BASE_URL — an exported key alone never triggers a call.
DEFAULT_MODEL = "deepseek-v4-flash"

#: Deterministic per-token prices in USD; the engine stamps its own
#: price_version at ingestion — this agent-side version is deliberately wrong
#: so the stamping is observable in cost summaries (§12A.5).
_PRICE_INPUT_PER_1M = 2.0
_PRICE_OUTPUT_PER_1M = 8.0
AGENT_PRICE_VERSION = "1999-12"


def _register(name: str):
    def deco(fn):
        TOOLS[name] = fn
        return fn

    return deco


@_register("search_articles")
def search_articles(query: str) -> dict:
    """Mock knowledge base: matching articles for the query."""
    return {
        "matches": [
            {"id": 1, "title": f"Article about {query}", "score": 0.95},
            {"id": 2, "title": f"More on {query}", "score": 0.80},
        ]
    }


@_register("check_refund_eligibility")
def check_refund_eligibility(order_id: str) -> dict:
    """Mock order system: refund eligibility for the order."""
    return {
        "order_id": order_id,
        "eligible": True,
        "amount": 49.99,
        "policy": "30-day-refund",
    }


class LlmError(Exception):
    """A chat-completions request failed; the message never carries the key."""


class LlmClient:
    """Minimal OpenAI-compatible chat completions client (stdlib urllib).
    ``base_url``/``model``/``api_key`` come from the environment — the same
    code runs offline against the test mock and live against DeepSeek."""

    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, messages: list[dict]) -> tuple[str, int, int]:
        """POST {base_url}/chat/completions. Returns
        (content, input_tokens, output_tokens); usage is read from the
        response, never assumed (§31B)."""
        body = json.dumps({"model": self.model, "messages": messages}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network, HTTP, or JSON errors
            raise LlmError(
                f"chat completions request failed ({exc.__class__.__name__})"
            ) from exc
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                "chat completions response has no choices[0].message.content"
            ) from exc
        usage = data.get("usage") or {}
        try:
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            input_tokens = output_tokens = 0
        return content, input_tokens, output_tokens


def _llm_config() -> tuple[str | None, str, str]:
    """(base_url, model, api_key). ``base_url`` is None when no provider is
    configured — the agent then uses the built-in mock classifier and never
    touches the network. The live path requires an explicit
    ``LLM_AGENT_EVAL_LLM_BASE_URL``, so an exported ``DEEPSEEK_API_KEY`` alone
    can never cause a live call."""
    base_url = os.environ.get("LLM_AGENT_EVAL_LLM_BASE_URL")
    model = os.environ.get("LLM_AGENT_EVAL_LLM_MODEL") or DEFAULT_MODEL
    api_key = os.environ.get("LLM_AGENT_EVAL_LLM_KEY") or os.environ.get(
        "DEEPSEEK_API_KEY", ""
    )
    return base_url, model, api_key


def _classify_offline(user_text: str) -> str:
    """The standalone fallback classifier: deterministic, no network."""
    if "refund" in user_text.lower():
        return "refund"
    return "question"


def _cost_block(input_tokens: int, output_tokens: int) -> CostBlock:
    cost_usd = (
        input_tokens * _PRICE_INPUT_PER_1M + output_tokens * _PRICE_OUTPUT_PER_1M
    ) / 1_000_000.0
    return CostBlock(
        tokens=TokenUsage(input=input_tokens, output=output_tokens),
        cost_usd=round(cost_usd, 8),
        currency="USD",
        price_version=AGENT_PRICE_VERSION,
    )


def _case_input() -> dict:
    """The case input: ``LLM_AGENT_EVAL_INPUT/case.json`` (§11A file
    protocol). Standalone: a default case."""
    input_dir = os.environ.get("LLM_AGENT_EVAL_INPUT")
    if input_dir:
        case_path = Path(input_dir) / "case.json"
        if case_path.exists():
            try:
                case = json.loads(case_path.read_text())
            except (OSError, ValueError):
                pass
            else:
                if isinstance(case, dict):
                    return case.get("input") or case
    return {
        "order_id": "ORD-STANDALONE",
        "customer_message": "I would like a refund.",
    }


def _write_result(payload: dict) -> None:
    """Atomic write: tmp -> fsync -> rename (§11A)."""
    output_dir = os.environ.get("LLM_AGENT_EVAL_OUTPUT")
    final = (
        Path(output_dir) / "result.json"
        if output_dir
        else Path(__file__).resolve().parent / "result.json"
    )
    tmp = final.with_name(final.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True))
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    tmp.rename(final)


def main() -> int:
    case = _case_input()
    order_id = str(case.get("order_id", "ORD-UNKNOWN"))
    message = str(case.get("customer_message", ""))
    base_url, model, api_key = _llm_config()
    client = LlmClient(base_url, model, api_key) if base_url else None

    with TraceCapture() as cap:
        cap.delegation("support_triage", "classify support case")

        if os.environ.get("LLM_AGENT_EVAL_MUTATE") == "1":
            # Fresh-process probe: one append per process. Each attempt must
            # observe a fresh module (engine §11A invariant).
            _STATE.append(order_id)
        state_len = len(_STATE)

        # 1) Triage: classify the customer request.
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a support-triage agent. Classify the customer request "
                    "and reply with a single JSON object: {\"intent\": "
                    "\"refund\"|\"question\", \"summary\": \"...\"}."
                ),
            },
            {"role": "user", "content": message},
        ]
        cap.llm_call(model, messages_hint=[{"role": "user", "content": message[:200]}])
        if client is not None:
            try:
                content, input_tokens, output_tokens = client.chat(messages)
            except LlmError as exc:
                # Honest failure: the runner maps this result to
                # provider_unreachable — never a fabricated pass (§11A).
                _write_result(
                    {
                        "status": "error",
                        "error": {
                            "type": "provider_unreachable",
                            "message": str(exc),
                            "retryable": True,
                        },
                    }
                )
                return 0
        else:
            content = json.dumps(
                {"intent": _classify_offline(message), "summary": "offline triage"}
            )
            input_tokens = output_tokens = 0
        cap.llm_response(
            model, content_hint=content[:200], cost=_cost_block(input_tokens, output_tokens)
        )
        try:
            intent = json.loads(content).get("intent", "question")
        except (ValueError, AttributeError):
            intent = "question"

        # 2) Tools: knowledge base always; eligibility for refund requests.
        if intent == "refund":
            cap.delegation("refund_specialist", f"resolve refund for {order_id}")
            cap.tool_call("search_articles", {"query": "refund policy"})
            articles = TOOLS["search_articles"]("refund policy")
            cap.tool_result("search_articles", articles)
            cap.tool_call("check_refund_eligibility", {"order_id": order_id})
            eligibility = TOOLS["check_refund_eligibility"](order_id)
            cap.tool_result("check_refund_eligibility", eligibility)
            final = {
                "intent": "refund",
                "order_id": order_id,
                "eligible": bool(eligibility.get("eligible")),
                "articles_found": len(articles.get("matches", [])),
            }
        else:
            cap.tool_call("search_articles", {"query": message})
            articles = TOOLS["search_articles"](message)
            cap.tool_result("search_articles", articles)
            final = {
                "intent": "question",
                "order_id": order_id,
                "articles_found": len(articles.get("matches", [])),
            }

        # 3) Compose the final answer (second cost-bearing LLM exchange).
        final_messages = [
            {
                "role": "system",
                "content": (
                    "Summarize the resolution as a JSON object with keys intent, "
                    "order_id, eligible."
                ),
            },
            {"role": "user", "content": json.dumps(final)},
        ]
        cap.llm_call(model, messages_hint=[{"role": "user", "content": "summarize resolution"}])
        if client is not None:
            try:
                content, input_tokens, output_tokens = client.chat(final_messages)
            except LlmError as exc:
                # Same honest failure as triage — a provider outage at compose
                # is provider_unreachable, never invocation_failed (§11A).
                _write_result(
                    {
                        "status": "error",
                        "error": {
                            "type": "provider_unreachable",
                            "message": str(exc),
                            "retryable": True,
                        },
                    }
                )
                return 0
        else:
            content = json.dumps(final)
            input_tokens = output_tokens = 0
        cap.llm_response(
            model, content_hint=content[:200], cost=_cost_block(input_tokens, output_tokens)
        )

        _write_result(
            {
                "status": "completed",
                "case_id": os.environ.get("ENGINE_CASE_ID", ""),
                "intent": intent,
                "eligible": bool(final.get("eligible", False)),
                "articles_found": final.get("articles_found"),
                "state_len": state_len,
                "final_response": final,
            }
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
