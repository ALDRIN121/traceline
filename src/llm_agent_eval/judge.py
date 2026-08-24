"""The judge as a validated instrument (semantics §15A / §15A.1).

:class:`JudgeEvaluator` is the interface every judge implementation satisfies;
this layer never inlines an LLM call and never touches the network — wiring
the real provider happens in a later chunk. The provisional
:class:`NoopJudge` ships with the layer: fully deterministic (fixed score, no
model, no network), which is exactly what "usable-but-provisional" means
before a provider is bound (§15A.1, owner decision 4J).

Judge verdicts are validated structured output (§15A.2): a verdict whose score
falls outside the rubric's declared scale is a :class:`JudgmentError` — an
ERROR case result (`on_error` per the metric, §7A.4), never a coerced score.

Readiness lives per judge binding — (provider, exact model id + version,
schema version, rubric version, §15A.5) — in the
UNCALIBRATED / CALIBRATING / CALIBRATED state machine (§15A.1.1). Transitions
are §15A.1.2's, with the shipped default thresholds (>= 30 labels and
kappa >= 0.6 to calibrate; kappa < 0.5 resets). UNCALIBRATED and CALIBRATING
scores are provisional — badged and excluded from gate and regression
enforcement; ``evaluators.gate_safe`` implements the exclusion.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum

from .spec import JudgeBinding, Metric, TestCase

__all__ = [
    "ReadinessState",
    "JudgmentError",
    "JudgeVerdict",
    "JudgeEvaluator",
    "NoopJudge",
    "ScriptedJudge",
    "JudgeReadiness",
    "JudgeReadinessRegistry",
    "readiness_is_provisional",
]

#: The shipped calibration thresholds (§15A.1.2; workspace-configurable, these
#: are what ships).
_DEFAULT_MIN_LABELS = 30
_DEFAULT_KAPPA_READY = 0.6
_DEFAULT_KAPPA_RESET = 0.5


class ReadinessState(str, Enum):
    """The three judge-binding states (§15A.1.1)."""

    UNCALIBRATED = "UNCALIBRATED"
    CALIBRATING = "CALIBRATING"
    CALIBRATED = "CALIBRATED"


def readiness_is_provisional(state: ReadinessState) -> bool:
    """§15A.1.1: UNCALIBRATED and CALIBRATING enforce identically — derived
    scores are badged provisional and excluded from gate enforcement."""
    return state in (ReadinessState.UNCALIBRATED, ReadinessState.CALIBRATING)


class JudgmentError(Exception):
    """A judge output violates the §15A.2 contract — an ERROR result (on_error
    per the metric), never a coerced score."""


@dataclass(frozen=True)
class JudgeVerdict:
    """The validated judge output (§15A.2): score within the rubric's declared
    scale, prose justification, and evidence refs (trace event ids).

    Construction validates the score: an out-of-scale (or non-numeric) score
    raises :class:`JudgmentError` — the platform never coerces one.
    """

    score: float
    justification: str
    evidence_refs: tuple[str, ...] = ()
    #: The rubric's declared scale the score is validated against; None lets
    #: the caller (evaluators) validate against scoring.range.
    scale: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise JudgmentError(f"judge score must be a number, got {self.score!r}")
        if self.scale is not None:
            lo, hi = self.scale
            if not (lo <= self.score <= hi):
                raise JudgmentError(
                    f"judge score {self.score!r} outside the rubric's declared scale {self.scale!r}"
                )


class JudgeEvaluator(ABC):
    """The judge interface. ``evaluate`` must be pure and deterministic from
    the caller's perspective (cassette or cache backed); the binding is the
    instrument identity (§15A.5) — two runs' judge scores are comparable only
    when bindings match."""

    @property
    @abstractmethod
    def binding(self) -> JudgeBinding:
        """The exact instrument: provider, model id + version, schema version,
        rubric version."""

    @abstractmethod
    def evaluate(self, metric: Metric, case: TestCase, evidence: tuple[str, ...]) -> JudgeVerdict:
        """Judge one case: return the §15A.2 verdict for ``metric`` over
        ``case``, with ``evidence`` the matched trace event ids made available
        as evidence refs. Raises JudgmentError on contract violations."""


class NoopJudge(JudgeEvaluator):
    """The provisional no-op judge: deterministic, no model, no network, no
    randomness. Returns a fixed score (default 0.5, the midpoint of [0, 1])
    with an UNCALIBRATED badge. This is the shipped behavior until a provider
    is wired — every score it produces is provisional by construction."""

    def __init__(
        self,
        binding: JudgeBinding,
        *,
        score: float = 0.5,
        justification: str = (
            "provisional no-op judge — UNCALIBRATED, no model wired (§15A.1)"
        ),
    ) -> None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"NoopJudge score must be a number, got {score!r}")
        self._binding = binding
        self._score = float(score)
        self._justification = justification

    @property
    def binding(self) -> JudgeBinding:
        return self._binding

    def evaluate(self, metric: Metric, case: TestCase, evidence: tuple[str, ...]) -> JudgeVerdict:
        return JudgeVerdict(self._score, self._justification, evidence)


class ScriptedJudge(JudgeEvaluator):
    """Deterministic judge for tests and fixtures: returns a per-case score
    from a script, defaulting to ``default_score`` for unlisted cases."""

    def __init__(
        self,
        binding: JudgeBinding,
        scores: dict[str, float] | None = None,
        *,
        default_score: float = 0.8,
        justification: str = "scripted judge (test fixture)",
    ) -> None:
        for value in (*(scores or {}).values(), default_score):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"scripted judge scores must be numbers, got {value!r}")
        self._binding = binding
        self._scores = dict(scores or {})
        self._default_score = float(default_score)
        self._justification = justification

    @property
    def binding(self) -> JudgeBinding:
        return self._binding

    def evaluate(self, metric: Metric, case: TestCase, evidence: tuple[str, ...]) -> JudgeVerdict:
        score = self._scores.get(case.case_id, self._default_score)
        return JudgeVerdict(score, self._justification, evidence)


@dataclass(frozen=True)
class JudgeReadiness:
    """One judge binding's readiness state (§15A.1). Frozen; transitions return
    new instances. Thresholds are workspace-configurable (stored next to the
    binding) — the shipped defaults are §15A.1.2's."""

    binding: JudgeBinding
    state: ReadinessState = ReadinessState.UNCALIBRATED
    label_count: int = 0
    kappa: float | None = None
    min_labels: int = _DEFAULT_MIN_LABELS
    kappa_ready: float = _DEFAULT_KAPPA_READY
    kappa_reset: float = _DEFAULT_KAPPA_RESET

    def __post_init__(self) -> None:
        if self.label_count < 0:
            raise ValueError(f"label_count must be >= 0, got {self.label_count}")

    @property
    def is_provisional(self) -> bool:
        """§15A.1.1: UNCALIBRATED and CALIBRATING enforce identically."""
        return readiness_is_provisional(self.state)

    @property
    def sufficient(self) -> bool:
        """The §15A.1.2 calibration condition: >= min_labels labels and
        kappa >= kappa_ready."""
        return (
            self.label_count >= self.min_labels
            and self.kappa is not None
            and self.kappa >= self.kappa_ready
        )

    def with_label(self, n: int = 1) -> "JudgeReadiness":
        """A human label landed (§15A.1.2: first label -> CALIBRATING; the set
        suffices -> CALIBRATED)."""
        return replace(self, label_count=self.label_count + n)._derive()

    def with_kappa(self, kappa: float) -> "JudgeReadiness":
        """κ recomputed (model or rubric version change, new labels arriving)."""
        return replace(self, kappa=kappa)._derive()

    def reset(self, binding: JudgeBinding | None = None) -> "JudgeReadiness":
        """New rubric version or new judge binding -> UNCALIBRATED (§15A.1.2);
        labels are discarded with the binding."""
        return JudgeReadiness(
            binding=binding if binding is not None else self.binding,
            min_labels=self.min_labels,
            kappa_ready=self.kappa_ready,
            kappa_reset=self.kappa_reset,
        )

    def _derive(self) -> "JudgeReadiness":
        """§15A.1.2 transitions from (labels, kappa):
        - κ < kappa_reset (0.5) on a recompute -> UNCALIBRATED (or CALIBRATING
          if labels remain);
        - labels >= min and κ >= kappa_ready (0.6) -> CALIBRATED;
        - labels > 0 -> CALIBRATING;
        - else UNCALIBRATED."""
        if self.kappa is not None and self.kappa < self.kappa_reset:
            state = ReadinessState.CALIBRATING if self.label_count > 0 else ReadinessState.UNCALIBRATED
        elif self.sufficient:
            state = ReadinessState.CALIBRATED
        elif self.label_count > 0:
            state = ReadinessState.CALIBRATING
        else:
            state = ReadinessState.UNCALIBRATED
        return replace(self, state=state)


class JudgeReadinessRegistry:
    """Per-binding readiness, keyed by the §15A.5 instrument identity
    (JudgeBinding is not hashable, so the registry keys a canonical tuple)."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str, str, str], JudgeReadiness] = {}

    @staticmethod
    def _key(binding: JudgeBinding) -> tuple[str, str, str, str]:
        return (binding.provider, binding.model, binding.schema_version, binding.rubric_version)

    def get(self, binding: JudgeBinding) -> JudgeReadiness:
        """The binding's readiness; a binding never seen before is UNCALIBRATED
        (§15A.1.2: new binding -> UNCALIBRATED)."""
        key = self._key(binding)
        if key not in self._states:
            self._states[key] = JudgeReadiness(binding=binding)
        return self._states[key]

    def set(self, readiness: JudgeReadiness) -> None:
        self._states[self._key(readiness.binding)] = readiness

    def record_label(self, binding: JudgeBinding, n: int = 1) -> JudgeReadiness:
        readiness = self.get(binding).with_label(n)
        self.set(readiness)
        return readiness

    def recompute_kappa(self, binding: JudgeBinding, kappa: float) -> JudgeReadiness:
        readiness = self.get(binding).with_kappa(kappa)
        self.set(readiness)
        return readiness

    def reset_binding(self, binding: JudgeBinding) -> JudgeReadiness:
        """A binding change (or rubric version change) resets this binding to
        UNCALIBRATED; other bindings keep their own states (§15A.1.1)."""
        readiness = self.get(binding).reset(binding)
        self.set(readiness)
        return readiness

    def provisional(self, binding: JudgeBinding) -> bool:
        return self.get(binding).is_provisional

    def __contains__(self, binding: JudgeBinding) -> bool:
        return self._key(binding) in self._states
