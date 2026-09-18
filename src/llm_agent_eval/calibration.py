"""Durable judge-binding calibration state and label lineage."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any
import uuid

from .auth import Actor
from .contracts import WorkflowError
from .judge import JudgeReadiness, ReadinessState
from .spec import JudgeBinding

__all__ = ["JudgeCalibrationService", "PersistentJudgeReadinessRegistry"]


def binding_key(binding: JudgeBinding) -> str:
    encoded = json.dumps(binding.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _label(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and (not value.strip() or len(value) > 255):
            raise WorkflowError("calibration labels must be bounded non-empty strings")
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise WorkflowError("calibration labels must be finite scalar values")


def _label_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cohen_kappa(pairs: list[tuple[Any, Any]]) -> float | None:
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(human == judge for human, judge in pairs) / total
    categories = {_label_key(human) for human, _ in pairs} | {_label_key(judge) for _, judge in pairs}
    expected = 0.0
    for category in categories:
        human_count = sum(_label_key(human) == category for human, _ in pairs)
        judge_count = sum(_label_key(judge) == category for _, judge in pairs)
        expected += (human_count / total) * (judge_count / total)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


class JudgeCalibrationService:
    MIN_LABELS = 30
    KAPPA_READY = 0.6
    KAPPA_RESET = 0.5

    def __init__(self, storage):
        self.storage = storage

    def get(self, actor: Actor, binding: JudgeBinding) -> JudgeReadiness:
        actor.require(workspace_id=actor.workspace_id)
        key = binding_key(binding)
        record = self.storage.get_judge_calibration(
            workspace_id=actor.workspace_id, binding_key=key,
        )
        if record is None:
            self.storage.upsert_judge_calibration(
                workspace_id=actor.workspace_id, binding_key=key,
                provider=binding.provider, model=binding.model,
                schema_version=binding.schema_version, rubric_version=binding.rubric_version,
                generation=0, state=ReadinessState.UNCALIBRATED.value,
                label_count=0, kappa=None, min_labels=self.MIN_LABELS,
                kappa_ready=self.KAPPA_READY, kappa_reset=self.KAPPA_RESET,
            )
            record = self.storage.get_judge_calibration(
                workspace_id=actor.workspace_id, binding_key=key,
            )
            assert record is not None
        return JudgeReadiness(
            binding=binding, state=ReadinessState(record.state),
            label_count=record.label_count, kappa=record.kappa,
            min_labels=record.min_labels, kappa_ready=record.kappa_ready,
            kappa_reset=record.kappa_reset, generation=record.generation,
        )

    def record_label(
        self, actor: Actor, binding: JudgeBinding, *, case_id: str,
        human_label: Any, judge_label: Any, label_id: str | None = None,
    ) -> JudgeReadiness:
        actor.require(write=True, workspace_id=actor.workspace_id)
        if not isinstance(case_id, str) or not case_id.strip():
            raise WorkflowError("case_id is required", code="calibration_label_invalid")
        human_label, judge_label = _label(human_label), _label(judge_label)
        current = self.get(actor, binding)
        key = binding_key(binding)
        self.storage.append_judge_calibration_label(
            workspace_id=actor.workspace_id, binding_key=key,
            generation=self._generation(actor, key), label_id=label_id or uuid.uuid4().hex,
            case_id=case_id, human_label=human_label, judge_label=judge_label,
            author_id=actor.actor_id,
        )
        return self._recompute(actor, binding, current)

    def reset(self, actor: Actor, binding: JudgeBinding) -> JudgeReadiness:
        actor.require(write=True, workspace_id=actor.workspace_id)
        key = binding_key(binding)
        current = self.get(actor, binding)
        record = self.storage.get_judge_calibration(
            workspace_id=actor.workspace_id, binding_key=key,
        )
        assert record is not None
        self.storage.upsert_judge_calibration(
            workspace_id=actor.workspace_id, binding_key=key,
            provider=binding.provider, model=binding.model,
            schema_version=binding.schema_version, rubric_version=binding.rubric_version,
            generation=record.generation + 1, state=ReadinessState.UNCALIBRATED.value,
            label_count=0, kappa=None, min_labels=current.min_labels,
            kappa_ready=current.kappa_ready, kappa_reset=current.kappa_reset,
        )
        return self.get(actor, binding)

    def _generation(self, actor: Actor, key: str) -> int:
        record = self.storage.get_judge_calibration(
            workspace_id=actor.workspace_id, binding_key=key,
        )
        assert record is not None
        return record.generation

    def _recompute(self, actor: Actor, binding: JudgeBinding, current: JudgeReadiness) -> JudgeReadiness:
        key = binding_key(binding)
        labels = self.storage.list_judge_calibration_labels(
            workspace_id=actor.workspace_id, binding_key=key,
            generation=self._generation(actor, key),
        )
        kappa = cohen_kappa([(row.human_label, row.judge_label) for row in labels])
        readiness = JudgeReadiness(
            binding=binding, label_count=len(labels), kappa=kappa,
            min_labels=current.min_labels, kappa_ready=current.kappa_ready,
            kappa_reset=current.kappa_reset, generation=self._generation(actor, key),
        )._derive()
        self.storage.upsert_judge_calibration(
            workspace_id=actor.workspace_id, binding_key=key,
            provider=binding.provider, model=binding.model,
            schema_version=binding.schema_version, rubric_version=binding.rubric_version,
            generation=self._generation(actor, key), state=readiness.state.value,
            label_count=readiness.label_count, kappa=readiness.kappa,
            min_labels=readiness.min_labels, kappa_ready=readiness.kappa_ready,
            kappa_reset=readiness.kappa_reset,
        )
        return readiness


class PersistentJudgeReadinessRegistry:
    """The small ``JudgeReadinessRegistry`` interface backed by storage."""

    def __init__(self, storage, actor: Actor):
        self.service = JudgeCalibrationService(storage)
        self.actor = actor

    def get(self, binding: JudgeBinding) -> JudgeReadiness:
        return self.service.get(self.actor, binding)
