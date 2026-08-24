"""Live DeepSeek round-trip — the coordinator runs these LAST.

Marked ``live`` (excluded by the default pytest config, ``-m 'not live'``)
and skipped without ``DEEPSEEK_API_KEY`` — they must never run inside the
offline suite and never fail the suite when the key is absent.
"""

from __future__ import annotations

import pytest

from llm_agent_eval.config import settings
from llm_agent_eval.gateway import DeepSeekGateway
from llm_agent_eval.harness import author_spec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.model.has_key,
        reason="DEEPSEEK_API_KEY not set (offline environment)",
    ),
]


def test_live_chat_round_trip():
    gateway = DeepSeekGateway(settings.model)
    try:
        response = gateway.chat(
            [{"role": "user", "content": "Reply with exactly one word: pong"}]
        )
    finally:
        gateway.close()
    assert response.content.strip()
    assert response.model
    assert response.input_tokens > 0
    assert response.output_tokens > 0


def test_live_chat_json_returns_object():
    gateway = DeepSeekGateway(settings.model)
    try:
        result = gateway.chat_json(
            [
                {"role": "system", "content": "You output ONLY JSON objects."},
                {"role": "user", "content": 'Return exactly {"ok": true} and nothing else.'},
            ]
        )
    finally:
        gateway.close()
    assert isinstance(result, dict)
    assert result == {"ok": True}


def test_live_author_spec_validates():
    """The LLM layer's core job, live: natural-language intent → a
    pydantic-validated EvaluationSpec, through the real DeepSeek API."""
    gateway = DeepSeekGateway(settings.model)
    try:
        result = author_spec(
            "Add a case where the refund amount is exactly 49.99 and the "
            "order is already refunded.",
            gateway,
            repair_attempts=2,
        )
    finally:
        gateway.close()
    assert result.validated, f"authoring failed: {result.reason}"
    assert result.spec is not None
    assert result.spec.cases, "validated spec must contain cases"
    assert result.inferred_share is not None
