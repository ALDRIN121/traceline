"""Tests for the judge instrument layer (src/llm_agent_eval/judge.py).

Covers §15A.2 verdict validation (out-of-scale scores are JudgmentError, never
coerced), the deterministic provisional judges (NoopJudge, ScriptedJudge), and
the §15A.1 readiness state machine: UNCALIBRATED -> CALIBRATING -> CALIBRATED
transitions per binding, the shipped thresholds (>= 30 labels, kappa >= 0.6
calibrated; kappa < 0.5 resets), and per-binding isolation in the registry.
"""

from __future__ import annotations

import pytest

from llm_agent_eval.judge import (
    JudgeReadiness,
    JudgeReadinessRegistry,
    JudgeVerdict,
    JudgmentError,
    NoopJudge,
    ReadinessState,
    ScriptedJudge,
    readiness_is_provisional,
)
from llm_agent_eval.spec import JudgeBinding, Metric, TestCase

BINDING = JudgeBinding(
    provider="anthropic",
    model="claude-opus-5",
    schema_version="1",
    rubric_version="1",
)

OTHER_BINDING = JudgeBinding(
    provider="anthropic",
    model="claude-opus-5",
    schema_version="1",
    rubric_version="2",  # different rubric -> different instrument
)


def make_metric() -> Metric:
    return Metric(
        metric_id="responsiveness",
        name="response quality",
        type="judge",
        target={"type": "final_response", "selector": "$.text", "on_missing": "fail"},
        evaluator={"type": "llm_judge", "rubric_version_id": "rubric_v1"},
        scoring={"type": "numeric", "range": [0, 1]},
        aggregation={"method": "mean", "on_error": "exclude"},
        judge_binding=BINDING,
    )


def make_case() -> TestCase:
    return TestCase(case_id="c1", name="c1", input={"order_id": "123"})


# ---------------------------------------------------------------------------
# JudgeVerdict (§15A.2)
# ---------------------------------------------------------------------------


def test_verdict_in_scale():
    verdict = JudgeVerdict(score=0.8, justification="good", scale=(0.0, 1.0))
    assert verdict.score == 0.8
    assert verdict.justification == "good"


def test_verdict_out_of_scale_raises():
    with pytest.raises(JudgmentError):
        JudgeVerdict(score=2.0, justification="too big", scale=(0.0, 1.0))


def test_verdict_non_numeric_raises():
    with pytest.raises(JudgmentError):
        JudgeVerdict(score="high", justification="not a number")


def test_verdict_bool_is_not_a_score():
    with pytest.raises(JudgmentError):
        JudgeVerdict(score=True, justification="bool is not numeric")


# ---------------------------------------------------------------------------
# Deterministic provisional judges
# ---------------------------------------------------------------------------


def test_noop_judge_is_deterministic():
    judge = NoopJudge(BINDING)
    first = judge.evaluate(make_metric(), make_case(), ("e1",))
    second = judge.evaluate(make_metric(), make_case(), ("e1",))
    assert first == second
    assert first.score == 0.5
    assert first.evidence_refs == ("e1",)
    assert judge.binding == BINDING


def test_noop_judge_refuses_non_numeric_score():
    with pytest.raises(ValueError):
        NoopJudge(BINDING, score="half")


def test_scripted_judge_returns_per_case_scores():
    judge = ScriptedJudge(BINDING, scores={"c1": 0.9}, default_score=0.2)
    assert judge.evaluate(make_metric(), make_case(), ()).score == 0.9
    other = TestCase(case_id="c2", name="c2", input={})
    assert judge.evaluate(make_metric(), other, ()).score == 0.2


def test_scripted_judge_evidence_passthrough():
    judge = ScriptedJudge(BINDING)
    verdict = judge.evaluate(make_metric(), make_case(), ("e1", "e2"))
    assert verdict.evidence_refs == ("e1", "e2")


# ---------------------------------------------------------------------------
# Readiness state machine (§15A.1.1 / §15A.1.2)
# ---------------------------------------------------------------------------


def test_new_binding_is_uncalibrated_and_provisional():
    readiness = JudgeReadiness(binding=BINDING)
    assert readiness.state is ReadinessState.UNCALIBRATED
    assert readiness.is_provisional is True
    assert readiness.sufficient is False


def test_first_label_moves_to_calibrating():
    readiness = JudgeReadiness(binding=BINDING).with_label(1)
    assert readiness.state is ReadinessState.CALIBRATING
    assert readiness.is_provisional is True  # CALIBRATING enforces like UNCALIBRATED


def test_label_threshold_and_kappa_calibrate():
    readiness = (
        JudgeReadiness(binding=BINDING).with_label(30).with_kappa(0.7)
    )
    assert readiness.state is ReadinessState.CALIBRATED
    assert readiness.is_provisional is False
    assert readiness.sufficient is True


def test_kappa_just_below_ready_stays_calibrating():
    readiness = (
        JudgeReadiness(binding=BINDING).with_label(30).with_kappa(0.59)
    )
    assert readiness.state is ReadinessState.CALIBRATING


def test_too_few_labels_stays_calibrating_even_with_kappa():
    readiness = (
        JudgeReadiness(binding=BINDING).with_label(5).with_kappa(0.9)
    )
    assert readiness.state is ReadinessState.CALIBRATING


def test_kappa_below_reset_resets_but_keeps_labels():
    # §15A.1.2: kappa < 0.5 on a recompute -> UNCALIBRATED (or CALIBRATING if
    # labels remain).
    readiness = (
        JudgeReadiness(binding=BINDING).with_label(30).with_kappa(0.7)
        .with_kappa(0.3)
    )
    assert readiness.state is ReadinessState.CALIBRATING
    assert readiness.label_count == 30


def test_kappa_below_reset_with_no_labels_is_uncalibrated():
    readiness = JudgeReadiness(binding=BINDING).with_kappa(0.3)
    assert readiness.state is ReadinessState.UNCALIBRATED


def test_reset_clears_labels():
    readiness = JudgeReadiness(binding=BINDING).with_label(40).with_kappa(0.8).reset()
    assert readiness.state is ReadinessState.UNCALIBRATED
    assert readiness.label_count == 0
    assert readiness.kappa is None


def test_negative_labels_rejected():
    with pytest.raises(ValueError):
        JudgeReadiness(binding=BINDING, label_count=-1)


# ---------------------------------------------------------------------------
# Registry — per-binding isolation (§15A.1.1)
# ---------------------------------------------------------------------------


def test_registry_get_unknown_binding_is_uncalibrated():
    registry = JudgeReadinessRegistry()
    assert BINDING not in registry  # untouched bindings are not stored
    assert registry.get(BINDING).state is ReadinessState.UNCALIBRATED
    # get() materializes the default state so repeated reads are stable.
    assert BINDING in registry


def test_registry_record_label_and_kappa_transition():
    registry = JudgeReadinessRegistry()
    registry.record_label(BINDING, 30)
    assert registry.get(BINDING).state is ReadinessState.CALIBRATING
    registry.recompute_kappa(BINDING, 0.8)
    assert registry.get(BINDING).state is ReadinessState.CALIBRATED
    assert registry.provisional(BINDING) is False


def test_registry_states_are_per_binding():
    registry = JudgeReadinessRegistry()
    registry.record_label(BINDING, 30)
    registry.recompute_kappa(BINDING, 0.8)  # CALIBRATED
    assert registry.get(OTHER_BINDING).state is ReadinessState.UNCALIBRATED
    assert registry.provisional(OTHER_BINDING) is True


def test_registry_reset_binding_affects_only_that_binding():
    registry = JudgeReadinessRegistry()
    registry.record_label(BINDING, 30)
    registry.recompute_kappa(BINDING, 0.8)
    registry.reset_binding(BINDING)
    assert registry.get(BINDING).state is ReadinessState.UNCALIBRATED
    assert BINDING in registry  # reset stores the fresh state


def test_registry_set_explicit_readiness():
    registry = JudgeReadinessRegistry()
    ready = JudgeReadiness(binding=BINDING, label_count=40, kappa=0.9)
    registry.set(ready)
    assert registry.get(BINDING) == ready


def test_readiness_is_provisional_helper():
    assert readiness_is_provisional(ReadinessState.UNCALIBRATED) is True
    assert readiness_is_provisional(ReadinessState.CALIBRATING) is True
    assert readiness_is_provisional(ReadinessState.CALIBRATED) is False
