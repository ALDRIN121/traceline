"""The judge wired to the ModelGateway (semantics §15A / §15A.1 / §15A.2).

:class:`GatewayJudge` implements the :class:`JudgeEvaluator` interface from
``judge.py`` with a live gateway (the deepseek binding by construction): the
rubric comes from the metric (``evaluator.rubric_version_id``), the judged
content is the case's input + expected values, the evidence is the case's
matched trace events. The binding — (provider, exact model id + version,
schema version, rubric version, §15A.5) — is **fixed at construction**;
judge calls never fall back to another model (§12C.4), because the gateway
is bound to exactly one model. State defaults to UNCALIBRATED (§15A.1.2: a
new binding is UNCALIBRATED); every verdict is provisional until calibration.

The verdict is validated structured output (§15A.2): the untrusted dict the
gateway returns is validated STRICTLY (no coercion — a numeric string is not
a score, extra keys are rejected). A score outside the rubric's declared
scale (the metric's ``scoring.range``) or a malformed payload is a
:class:`JudgmentError` — an ERROR result (``on_error`` per the metric),
never a coerced score. A gateway failure surfaces as JudgmentError too: an
infrastructure failure is an ERROR result, never a silent pass.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .gateway import GatewayError, ModelGateway
from .judge import (
    JudgeEvaluator,
    JudgeReadiness,
    JudgeVerdict,
    JudgmentError,
    readiness_is_provisional,
)
from .spec import JudgeBinding, Metric, TestCase

__all__ = ["GatewayJudge", "JUDGE_OUTPUT_SCHEMA"]

#: The §15A.2 output contract — the judge's structured output schema (also
#: passed to the gateway as the JSON-mode hint).
JUDGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "justification": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "justification", "evidence_refs"],
    "additionalProperties": False,
}


class _VerdictOutput(BaseModel):
    """The validated judge output (§15A.2). STRICT: no coercion — a numeric
    string is not a score, extra keys are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    score: float
    justification: str
    evidence_refs: list[str] = []


_JUDGE_SYSTEM = (
    "You are an evaluation judge for agentic systems (semantics §15A). You "
    "receive one judgment unit: a rubric reference, the candidate's content, "
    "and the evidence refs made available. Emit ONLY ONE JSON object with "
    "exactly the keys 'score' (a number within the rubric's declared scale), "
    "'justification' (prose explaining the score), and 'evidence_refs' (the "
    "refs you used). Never emit code, never emit text outside the object."
)


def _judge_unit(metric: Metric, case: TestCase, evidence: tuple[str, ...]) -> dict[str, Any]:
    """The §15A.2 judgment unit: {rubric_version_id, content, evidence_refs}."""
    rubric_version_id = metric.evaluator.rubric_version_id
    if not rubric_version_id:
        raise JudgmentError("judge metric requires evaluator.rubric_version_id (§15A)")
    content = {
        "input": case.input,
        "expected": {key: ev.model_dump() for key, ev in case.expected.items()},
    }
    return {
        "rubric_version_id": rubric_version_id,
        "content": content,
        "evidence_refs": list(evidence),
    }


class GatewayJudge(JudgeEvaluator):
    """The judge bound to one gateway — the deepseek binding by construction
    (``provider``/``model`` come from the gateway's config; the default model
    is ``deepseek-v4-flash``). Binding fixed at construction; readiness
    defaults to UNCALIBRATED (§15A.1.2)."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        rubric_version: str = "1",
        schema_version: str = "1",
        readiness: JudgeReadiness | None = None,
    ) -> None:
        if not isinstance(gateway, ModelGateway):
            raise TypeError(f"gateway must be a ModelGateway, got {type(gateway).__name__}")
        self._gateway = gateway
        self._binding = JudgeBinding(
            provider=gateway.provider,
            model=gateway.model,
            schema_version=schema_version,
            rubric_version=rubric_version,
        )
        self._readiness = (
            readiness if readiness is not None else JudgeReadiness(binding=self._binding)
        )

    @property
    def binding(self) -> JudgeBinding:
        """The §15A.5 instrument identity — fixed at construction."""
        return self._binding

    @property
    def readiness(self) -> JudgeReadiness:
        """Per-binding readiness; UNCALIBRATED by default (§15A.1.2)."""
        return self._readiness

    @property
    def is_provisional(self) -> bool:
        """§15A.1.1: UNCALIBRATED and CALIBRATING scores are provisional —
        badged and excluded from gate and regression enforcement."""
        return readiness_is_provisional(self._readiness.state)

    def evaluate(self, metric: Metric, case: TestCase, evidence: tuple[str, ...]) -> JudgeVerdict:
        unit = _judge_unit(metric, case, evidence)
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(unit)},
        ]
        try:
            raw = self._gateway.chat_json(messages, JUDGE_OUTPUT_SCHEMA)
        except GatewayError as exc:
            # An infrastructure failure is an ERROR result (on_error per the
            # metric) — never a coerced score, never a silent pass.
            raise JudgmentError(f"judge gateway unreachable: {exc}") from exc
        try:
            output = _VerdictOutput.model_validate(raw)
        except ValidationError as exc:
            raise JudgmentError(f"judge output violates the §15A.2 contract: {exc}") from exc
        scale = tuple(metric.scoring.range) if metric.scoring.range is not None else None
        refs = tuple(output.evidence_refs) if output.evidence_refs else tuple(evidence)
        # JudgeVerdict validates the score against the rubric's declared
        # scale — an out-of-scale score is JudgmentError, never coerced.
        return JudgeVerdict(
            score=output.score,
            justification=output.justification,
            evidence_refs=refs,
            scale=scale,
        )
