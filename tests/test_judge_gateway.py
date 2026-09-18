"""Tests for the gateway-backed judge (src/llm_agent_eval/judge_gateway.py).

Offline (MockGateway): the §15A.5 binding fixed at construction, the
§15A.1.1 UNCALIBRATED default and provisional flag, the §15A.2 verdict
contract (validated strictly — never coerced), rubric-from-the-metric,
evidence passthrough, and gateway failures surfacing as JudgmentError (an
ERROR result, never a silent pass).
"""

from __future__ import annotations

import json

import pytest

from llm_agent_eval.gateway import GatewayError, MockGateway
from llm_agent_eval.judge import JudgmentError, ReadinessState
from llm_agent_eval.judge_gateway import GatewayJudge, JudgmentContext, JUDGE_OUTPUT_SCHEMA
from llm_agent_eval.rubric_store import RubricContent
# TestCase aliased so pytest does not attempt to collect the pydantic class.
from llm_agent_eval.spec import Metric, TestCase as SpecTestCase

BINDING_KWARGS = dict(provider="deepseek", model="deepseek-v4-flash")


def make_gateway(json_handler, **gateway_kwargs):
    return MockGateway({"chat_json": json_handler}, **gateway_kwargs)


RUBRIC = RubricContent(rubric_id="rubric_v1", instructions="Judge quality.", candidate_fields=["final_output"], evidence_ids=["evt_1", "evt_2", "evt_3"])


def make_judge(json_handler=None, **judge_kwargs):
    handler = json_handler or (lambda m, h: {"score": 0.87, "justification": "good", "evidence_refs": []})
    gateway = make_gateway(handler, **BINDING_KWARGS)
    kwargs = {
        "rubric_resolver": lambda rubric_id: RUBRIC,
        "context_resolver": lambda metric, case, evidence: JudgmentContext(
            candidate_output="the candidate response",
            evidence={"evt_1": "tool call", "evt_2": "provider reply", "evt_3": "final answer"},
        ),
    }
    kwargs.update(judge_kwargs)
    return GatewayJudge(gateway, **kwargs), gateway


def make_metric(**overrides):
    metric = {
        "metric_id": "responsiveness",
        "name": "response quality",
        "type": "judge",
        "target": {"type": "final_response", "selector": "$.text", "on_missing": "fail"},
        "evaluator": {"type": "llm_judge", "rubric_version_id": "rubric_v1"},
        "scoring": {"type": "numeric", "range": [0, 1]},
        "aggregation": {"method": "mean", "on_error": "exclude"},
        "judge_binding": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "schema_version": "1",
            "rubric_version": "1",
        },
    }
    metric.update(overrides)
    return Metric.model_validate(metric)


def make_case(**overrides):
    fields = {"case_id": "c1", "name": "c1", "input": {"order_id": "123"}}
    fields.update(overrides)
    return SpecTestCase.model_validate(fields)


# ---------------------------------------------------------------------------
# Binding (§15A.5) and readiness (§15A.1.1)
# ---------------------------------------------------------------------------


def test_binding_fixed_at_construction():
    judge, _ = make_judge(rubric_version="2", schema_version="1")
    binding = judge.binding
    assert binding.provider == "deepseek"
    assert binding.model == "deepseek-v4-flash"  # exact model id from the gateway
    assert binding.schema_version == "1"
    assert binding.rubric_version == "2"


def test_default_state_is_uncalibrated_and_provisional():
    judge, _ = make_judge()
    assert judge.readiness.state is ReadinessState.UNCALIBRATED
    assert judge.is_provisional is True  # §15A.1: badged, excluded from gates


def test_non_model_gateway_rejected():
    with pytest.raises(TypeError, match="ModelGateway"):
        GatewayJudge("not a gateway")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Verdict contract (§15A.2)
# ---------------------------------------------------------------------------


def test_verdict_shape():
    judge, gateway = make_judge(
        lambda m, h: {"score": 0.87, "justification": "good", "evidence_refs": ["evt_1"]}
    )
    verdict = judge.evaluate(make_metric(), make_case(), ("evt_1", "evt_2"))
    assert verdict.score == 0.87
    assert verdict.justification == "good"
    assert verdict.evidence_refs == ("evt_1",)


def test_rubric_comes_from_the_metric():
    judge, gateway = make_judge()
    metric = make_metric(
        evaluator={"type": "llm_judge", "rubric_version_id": "rubric_v42"}
    )
    judge.evaluate(metric, make_case(), ())
    kind, messages, hint = gateway.calls[0]
    assert kind == "chat_json"
    unit = json.loads(messages[1]["content"])
    assert unit["rubric_version_id"] == "rubric_v42"
    assert hint == JUDGE_OUTPUT_SCHEMA  # the §15A.2 output schema was handed over


def test_evidence_is_the_cases_matched_events():
    judge, gateway = make_judge()
    judge.evaluate(make_metric(), make_case(), ("evt_1", "evt_2", "evt_3"))
    unit = json.loads(gateway.calls[0][1][1]["content"])
    assert unit["evidence_refs"] == ["evt_1", "evt_2", "evt_3"]
    assert json.loads(unit["content"]["input"]["text"]) == {"order_id": "123"}
    assert unit["rubric"]["instructions"] == "Judge quality."
    assert json.loads(unit["candidate_output"]["text"]) == "the candidate response"
    assert json.loads(unit["evidence"]["evt_1"]["text"]) == "tool call"


def test_out_of_scale_score_raises_never_coerced():
    # A score outside the rubric's declared scale (scoring.range) is an ERROR
    # result — never coerced (§15A.2).
    judge, _ = make_judge(lambda m, h: {"score": 2.0, "justification": "big", "evidence_refs": []})
    with pytest.raises(JudgmentError, match="scale"):
        judge.evaluate(make_metric(), make_case(), ())


def test_string_score_is_not_coerced():
    # Strict validation: a numeric string is not a score (§15A.2).
    judge, _ = make_judge(lambda m, h: {"score": "0.8", "justification": "x", "evidence_refs": []})
    with pytest.raises(JudgmentError):
        judge.evaluate(make_metric(), make_case(), ())


def test_extra_keys_rejected():
    judge, _ = make_judge(
        lambda m, h: {"score": 0.5, "justification": "x", "evidence_refs": [], "hack": 1}
    )
    with pytest.raises(JudgmentError):
        judge.evaluate(make_metric(), make_case(), ())


def test_missing_fields_rejected():
    judge, _ = make_judge(lambda m, h: {"score": 0.5})
    with pytest.raises(JudgmentError):
        judge.evaluate(make_metric(), make_case(), ())


def test_empty_model_evidence_does_not_fabricate_citations():
    judge, _ = make_judge(lambda m, h: {"score": 0.6, "justification": "x", "evidence_refs": []})
    verdict = judge.evaluate(make_metric(), make_case(), ("evt_1", "evt_2"))
    assert verdict.evidence_refs == ()


def test_gateway_failure_is_judgment_error():
    def boom(messages, hint):
        raise GatewayError("connection refused")

    judge, _ = make_judge(boom)
    with pytest.raises(JudgmentError, match="gateway"):
        judge.evaluate(make_metric(), make_case(), ())


def test_non_judge_metric_rejected():
    # A metric without evaluator.rubric_version_id cannot be judged (§15A).
    judge, _ = make_judge()
    scalar = Metric.model_validate(
        {
            "metric_id": "m",
            "name": "n",
            "type": "scalar",
            "target": {"type": "tool_output", "tool": "t", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": 1},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
        }
    )
    with pytest.raises(JudgmentError, match="rubric_version_id"):
        judge.evaluate(scalar, make_case(), ())
