"""Tests for the harness authoring flow (src/llm_agent_eval/harness.py).

Offline (MockGateway): the happy path intent -> validated EvaluationSpec,
the repair loop (an invalid first draft -> validation problems fed back ->
valid spec, with the attempt count), the explicit ``spec_draft_failed``
state when the spec never validates or the gateway is unreachable, and the
§10B.2 expected-value provenance enforcement with the dataset-health figure.
"""

from __future__ import annotations

import pytest

from llm_agent_eval.config import settings
from llm_agent_eval.gateway import GatewayError, MockGateway
from llm_agent_eval.harness import AuthoringResult, author_spec
from llm_agent_eval.spec import EvaluationSpec, ExpectedValue, SpecValidationError


def make_spec_dict(**overrides):
    """A fully valid spec (mirrors tests/test_spec.py's helper)."""
    spec = {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "cases": [
            {
                "case_id": "c1",
                "name": "eligible refund",
                "description": "refund for an eligible order",
                "input": {"order_id": "123"},
                "expected": {
                    "refund_amount": {"tag": "user_stated", "value": 49.99},
                },
            }
        ],
        "metrics": [
            {
                "metric_id": "refund_amount",
                "name": "refund amount correct",
                "type": "scalar",
                "target": {
                    "type": "tool_output",
                    "tool": "refund_order",
                    "selector": "$.amount",
                    "occurrence": "last",
                    "on_missing": "fail",
                },
                "evaluator": {"type": "numeric", "expected": 49.99, "tolerance": 0},
                "scoring": {"type": "binary", "range": [0, 1], "condition": "raw_value <= 50"},
                "aggregation": {"method": "pass_rate", "on_error": "fail"},
                "gate": {"min": 1.0},
                "weight": 1.0,
            }
        ],
    }
    spec.update(overrides)
    return spec


def make_broken_spec_dict(**overrides):
    """A spec that fails validation: the target omits on_missing (no default,
    §7A.3)."""
    spec = make_spec_dict()
    spec["metrics"][0]["target"] = {
        "type": "tool_output",
        "tool": "refund_order",
        "selector": "$.amount",
    }
    spec.update(overrides)
    return spec


INTENT = "Add a case where the refund amount is exactly 49.99 and the order is already refunded."


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_author_spec_happy_path_returns_validated_spec():
    gateway = MockGateway({"chat_json": lambda m, h: make_spec_dict()})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.validated is True
    assert result.repair_attempts == 0
    assert result.reason is None
    spec = result.spec
    assert isinstance(spec, EvaluationSpec)
    assert spec.name == "refund-suite"
    assert spec.spec_version == "1"
    assert spec.dataset_version == "v1"
    assert spec.metrics[0].metric_id == "refund_amount"
    # The only artifact is the pydantic-validated spec — models, not dicts.
    assert spec.cases[0].expected["refund_amount"].tag == "user_stated"


def test_author_spec_passes_intent_and_schema_to_gateway():
    gateway = MockGateway({"chat_json": lambda m, h: make_spec_dict()})
    author_spec(INTENT, gateway)
    kind, messages, hint = gateway.calls[0]
    assert kind == "chat_json"
    assert hint is not None  # the EvaluationSpec JSON Schema was handed over
    joined = " ".join(m["content"] for m in messages)
    assert INTENT in joined
    assert "on_missing" in joined  # the drafting rules are in the prompt


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------


def test_repair_loop_feeds_problems_back_and_repairs():
    sequence = [
        lambda m, h: make_broken_spec_dict(),
        lambda m, h: make_spec_dict(),
    ]
    gateway = MockGateway({"chat_json": sequence})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.repair_attempts == 1
    assert len(gateway.calls) == 2  # one draft + one repair
    # The repair call carried the previous draft and the validation problems.
    kind, messages, hint = gateway.calls[1]
    assert kind == "chat_json"
    joined = " ".join(m["content"] for m in messages)
    assert "on_missing" in joined  # the problem text was fed back
    assert "previous draft" in joined


def test_repair_loop_never_validates_fails_explicitly():
    gateway = MockGateway({"chat_json": lambda m, h: make_broken_spec_dict()})
    result = author_spec(INTENT, gateway)
    assert result.state == "spec_draft_failed"
    assert result.validated is False
    assert result.spec is None  # never a partial spec
    # Default config: 1 draft + 2 repairs.
    assert result.repair_attempts == settings.harness.spec_repair_attempts == 2
    assert len(gateway.calls) == 3
    assert "on_missing" in result.reason


def test_repair_attempts_override_zero():
    gateway = MockGateway({"chat_json": lambda m, h: make_broken_spec_dict()})
    result = author_spec(INTENT, gateway, repair_attempts=0)
    assert result.state == "spec_draft_failed"
    assert result.repair_attempts == 0
    assert len(gateway.calls) == 1  # only the initial draft was attempted


def test_gateway_unreachable_is_failure_state():
    def boom(messages, hint):
        raise GatewayError("connection refused")

    gateway = MockGateway({"chat_json": boom})
    result = author_spec(INTENT, gateway)
    assert result.state == "spec_draft_failed"
    assert result.spec is None
    assert result.reason is not None and "gateway" in result.reason


# ---------------------------------------------------------------------------
# §10B.2 expected-value provenance and the dataset-health figure
# ---------------------------------------------------------------------------


def test_inferred_tag_health_figure():
    spec_dict = make_spec_dict()
    spec_dict["cases"][0]["expected"] = {
        "refund_amount": {"tag": "user_stated", "value": 49.99},  # stated in intent
        "refund_status": {"tag": "inferred", "value": "refunded"},  # LLM-invented
        "currency": {"tag": "inferred", "value": "USD"},  # LLM-invented
    }
    gateway = MockGateway({"chat_json": lambda m, h: spec_dict})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.expected_count == 3
    assert result.inferred_expected == 2
    assert result.inferred_share == pytest.approx(2 / 3)  # §10B.2 health figure
    tags = {k: v.tag for k, v in result.spec.cases[0].expected.items()}
    assert tags == {"refund_amount": "user_stated", "refund_status": "inferred", "currency": "inferred"}


def test_user_stated_not_in_intent_is_retagged_inferred():
    # The LLM claims user_stated for a value the intent never states — the
    # harness re-tags it inferred (§10B.2: never silently authoritative).
    spec_dict = make_spec_dict()
    spec_dict["cases"][0]["expected"] = {
        "refund_amount": {"tag": "user_stated", "value": 99.99},  # not in INTENT
    }
    gateway = MockGateway({"chat_json": lambda m, h: spec_dict})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.spec.cases[0].expected["refund_amount"].tag == "inferred"
    assert result.inferred_expected == 1
    assert result.inferred_share == 1.0


def test_user_stated_in_intent_is_honored():
    # A value the intent really states keeps its user_stated tag — the
    # calibration authority for judging (§10B.2).
    spec_dict = make_spec_dict()
    spec_dict["cases"][0]["expected"] = {
        "refund_amount": {"tag": "user_stated", "value": 49.99},  # in INTENT
    }
    gateway = MockGateway({"chat_json": lambda m, h: spec_dict})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.spec.cases[0].expected["refund_amount"].tag == "user_stated"
    assert result.inferred_expected == 0
    assert result.inferred_share == 0.0


def test_derived_from_trace_is_retagged_inferred():
    # This authoring flow has no trace evidence — the harness never lets an
    # LLM claim derived_from_trace provenance it cannot have (§10B.1).
    spec_dict = make_spec_dict()
    spec_dict["cases"][0]["expected"] = {
        "refund_amount": {"tag": "derived_from_trace", "value": 49.99},
    }
    gateway = MockGateway({"chat_json": lambda m, h: spec_dict})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.spec.cases[0].expected["refund_amount"].tag == "inferred"
    assert result.inferred_expected == 1


def test_spec_without_expectations_has_no_health_figure():
    spec_dict = make_spec_dict()
    spec_dict["cases"][0]["expected"] = {}
    gateway = MockGateway({"chat_json": lambda m, h: spec_dict})
    result = author_spec(INTENT, gateway)
    assert result.state == "validated"
    assert result.expected_count == 0
    assert result.inferred_expected == 0
    assert result.inferred_share is None


# ---------------------------------------------------------------------------
# Input guards
# ---------------------------------------------------------------------------


def test_empty_intent_rejected():
    gateway = MockGateway({"chat_json": lambda m, h: make_spec_dict()})
    with pytest.raises(ValueError, match="intent"):
        author_spec("   ", gateway)
    with pytest.raises(ValueError, match="intent"):
        author_spec(123, gateway)


def test_negative_repair_attempts_rejected():
    gateway = MockGateway({"chat_json": lambda m, h: make_spec_dict()})
    with pytest.raises(ValueError, match="repair_attempts"):
        author_spec(INTENT, gateway, repair_attempts=-1)
