"""The harness authoring layer (harness §31A / §10B, §20.2).

:func:`author_spec` turns a natural-language intent into a pydantic-validated
EvaluationSpec: draft prompt -> ``chat_json`` -> ``validate_spec`` -> repair
loop (<= ``spec_repair_attempts`` repairs, feeding
:class:`SpecValidationError` problems back into the prompt). The validated
spec is the ONLY artifact that leaves the harness; every other outcome is
the explicit ``spec_draft_failed`` state — never a partial spec, never a
success the harness did not observe (STYLE invariant 11).

Governing rule (§10B.2): **an LLM-inferred expected value is never silently
authoritative.** This flow has no trace evidence, so ``derived_from_trace``
is never claimable here; an expectation the LLM tagged ``user_stated`` is
honored only when the intent text actually states the value — otherwise the
harness re-tags it ``inferred``. The dataset-health figure (count and share
of inferred expectations, §10B.2) is reported on the result.

Prompts are ephemeral: they exist only inside this module's repair loop and
are never persisted (harness chat messages are subject to redaction-at-write
in the full design — this layer stores nothing). The harness is the
authoring/explanation layer; it never scores and never stores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .config import settings
from .gateway import GatewayError, ModelGateway
from .spec import (
    EvaluationSpec,
    ExpectedValue,
    SpecProblem,
    SpecValidationError,
    validate_spec,
)

__all__ = ["author_spec", "AuthoringResult", "spec_json_schema"]

#: The JSON Schema the LLM drafts against (STYLE invariant 2: structured
#: output — the schema is the stabilizer, never sampling parameters).
spec_json_schema: dict[str, Any] = EvaluationSpec.model_json_schema()

_DRAFT_SYSTEM = (
    "You author validated evaluation specifications for agentic systems. "
    "Your entire output is ONE JSON object that validates against the "
    "provided schema — no markdown, no code fences, no text outside the "
    "object, no code. Rules you must follow: "
    "(1) Tag every expected value with its provenance: 'user_stated' ONLY "
    "when the user's intent explicitly states that exact value; otherwise "
    "'inferred'. Never claim 'derived_from_trace' — you have no trace "
    "evidence in this flow. "
    "(2) 'on_missing' (on the target) and 'on_error' (on the aggregation) "
    "are REQUIRED on every metric — they have no defaults. "
    "(3) Every case needs a case_id and a non-empty name. "
    "(4) A judge metric requires evaluator.type 'llm_judge' with "
    "'rubric_version_id' and a 'judge_binding' (provider, model, "
    "schema_version, rubric_version). "
    "(5) Never invent provenance you cannot observe."
)


def _user_intent_prompt(intent: str) -> str:
    return (
        "Author the evaluation spec for this intent:\n\n"
        f"{intent}\n\n"
        "Emit the spec as a single JSON object conforming to this schema:\n"
        f"{json.dumps(spec_json_schema, sort_keys=True)}"
    )


def _repair_prompt(problems: list[SpecProblem], previous: dict[str, Any]) -> str:
    detail = "\n".join(f"- {p.field}: {p.message}" for p in problems)
    return (
        "Your previous draft was rejected by schema validation with these "
        f"problems:\n{detail}\n\n"
        "Your previous draft was:\n"
        f"{json.dumps(previous, sort_keys=True)}\n\n"
        "Emit the COMPLETE corrected spec as a single JSON object fixing "
        "every listed problem. Do not explain, do not summarize, do not "
        "quote the problems back — the object must validate."
    )


def _draft_messages(intent: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user", "content": _user_intent_prompt(intent)},
    ]


def _repair_messages(
    intent: str,
    problems: list[SpecProblem],
    previous: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user", "content": _user_intent_prompt(intent)},
        {"role": "assistant", "content": json.dumps(previous)},
        {"role": "user", "content": _repair_prompt(problems, previous)},
    ]


def _value_mentioned(value: Any, intent_text: str) -> bool:
    """Does the intent text state this expected value? A heuristic — the
    harness can attribute a value to the user only from their words."""
    if isinstance(value, bool):
        return "true" in intent_text if value else "false" in intent_text
    if isinstance(value, (int, float)):
        return str(value) in intent_text
    if isinstance(value, str):
        return value.lower() in intent_text
    return False  # composite values cannot be attributed to an intent string


def _correct_expected_tags(spec: EvaluationSpec, intent: str) -> EvaluationSpec:
    """§10B.2 enforcement: an LLM-invented expected value is never silently
    authoritative. This authoring flow has no trace evidence, so a
    ``derived_from_trace`` claim is always re-tagged ``inferred``; a
    ``user_stated`` tag is honored only when the intent text states the
    value, and otherwise re-tagged ``inferred``."""
    text = intent.lower()
    changed = False
    new_cases: list[Any] = []
    for case in spec.cases:
        new_expected: dict[str, ExpectedValue] = {}
        case_changed = False
        for key, ev in case.expected.items():
            tag = ev.tag
            if tag == "derived_from_trace":
                tag = "inferred"
            elif tag == "user_stated" and not _value_mentioned(ev.value, text):
                tag = "inferred"
            if tag is not ev.tag:
                ev = ExpectedValue(tag=tag, value=ev.value)
                case_changed = True
            new_expected[key] = ev
        if case_changed:
            changed = True
            new_cases.append(case.model_copy(update={"expected": new_expected}))
        else:
            new_cases.append(case)
    if not changed:
        return spec
    return spec.model_copy(update={"cases": new_cases})


def _count_expected(spec: EvaluationSpec) -> tuple[int, int]:
    """(total expectations, expectations tagged inferred) across all cases."""
    total = 0
    inferred = 0
    for case in spec.cases:
        for ev in case.expected.values():
            total += 1
            if ev.tag == "inferred":
                inferred += 1
    return total, inferred


@dataclass(frozen=True)
class AuthoringResult:
    """The explicit authoring outcome. ``validated`` carries the ONLY
    artifact that leaves the harness — a pydantic-validated EvaluationSpec.
    ``spec_draft_failed`` means the gateway was unreachable or the spec never
    validated: never a partial spec, never a success not observed."""

    state: Literal["validated", "spec_draft_failed"]
    spec: EvaluationSpec | None = None
    repair_attempts: int = 0
    reason: str | None = None
    expected_count: int = 0
    inferred_expected: int = 0

    @property
    def validated(self) -> bool:
        return self.state == "validated"

    @property
    def inferred_share(self) -> float | None:
        """§10B.2 dataset-health figure: the share of expectations tagged
        inferred (0.0-1.0); None when the spec has no expectations."""
        if self.expected_count == 0:
            return None
        return self.inferred_expected / self.expected_count


def author_spec(
    intent: str,
    gateway: ModelGateway,
    *,
    repair_attempts: int | None = None,
) -> AuthoringResult:
    """Turn a natural-language ``intent`` into a validated EvaluationSpec.

    One initial draft plus up to ``repair_attempts`` repairs (default from
    ``config.harness.spec_repair_attempts``); each repair feeds the previous
    draft and the validation problems back into the prompt. The validated
    spec is the only artifact that leaves the harness; every other outcome
    is the explicit ``spec_draft_failed`` state.
    """
    if not isinstance(intent, str) or not intent.strip():
        raise ValueError("intent must be a non-empty string")
    if repair_attempts is None:
        repair_attempts = settings.harness.spec_repair_attempts
    if not isinstance(repair_attempts, int) or isinstance(repair_attempts, bool) or repair_attempts < 0:
        raise ValueError(f"repair_attempts must be a non-negative int, got {repair_attempts!r}")

    messages = _draft_messages(intent)
    previous: dict[str, Any] | None = None
    problems: list[SpecProblem] = []
    for attempt in range(repair_attempts + 1):
        try:
            draft = gateway.chat_json(messages, spec_json_schema)
        except GatewayError as exc:
            return AuthoringResult(
                state="spec_draft_failed",
                repair_attempts=attempt,
                reason=f"gateway unreachable: {exc}",
            )
        try:
            spec = validate_spec(draft)
        except SpecValidationError as exc:
            problems = exc.problems
            previous = draft
            messages = _repair_messages(intent, problems, previous)
            continue
        spec = _correct_expected_tags(spec, intent)
        total, inferred = _count_expected(spec)
        return AuthoringResult(
            state="validated",
            spec=spec,
            repair_attempts=attempt,
            expected_count=total,
            inferred_expected=inferred,
        )
    detail = "; ".join(f"{p.field}: {p.message}" for p in problems)
    return AuthoringResult(
        state="spec_draft_failed",
        repair_attempts=repair_attempts,
        reason=f"spec never validated after {repair_attempts} repair attempt(s): {detail}",
    )
