from __future__ import annotations

from llm_agent_eval.auth import Actor
from llm_agent_eval.judge import ReadinessState
from llm_agent_eval.spec import JudgeBinding
from llm_agent_eval.storage import Storage


def _binding(model: str = "judge-v1") -> JudgeBinding:
    return JudgeBinding(
        provider="fixture", model=model, schema_version="schema-1", rubric_version="rubric-1",
    )


def test_calibration_labels_persist_and_calibrate_at_threshold(tmp_path):
    from llm_agent_eval.calibration import JudgeCalibrationService

    db = tmp_path / "calibration.db"
    actor = Actor("owner", "ws", "owner")
    storage = Storage(db)
    storage.create_schema()
    service = JudgeCalibrationService(storage)
    binding = _binding()

    assert service.get(actor, binding).state is ReadinessState.UNCALIBRATED
    for index in range(30):
        readiness = service.record_label(
            actor, binding, case_id=f"case-{index}", human_label="pass", judge_label="pass",
        )
    assert readiness.state is ReadinessState.CALIBRATED
    assert readiness.label_count == 30
    assert readiness.kappa == 1.0
    storage.close()

    reopened = Storage(db)
    reopened.create_schema()
    persisted = JudgeCalibrationService(reopened).get(actor, binding)
    assert persisted.state is ReadinessState.CALIBRATED
    assert persisted.label_count == 30
    assert persisted.kappa == 1.0
    reopened.close()


def test_calibration_binding_and_generation_are_isolated(tmp_path):
    from llm_agent_eval.calibration import JudgeCalibrationService

    storage = Storage(tmp_path / "calibration.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    service = JudgeCalibrationService(storage)
    first = _binding("judge-v1")
    second = _binding("judge-v2")
    for index in range(3):
        service.record_label(actor, first, case_id=f"case-{index}", human_label=1, judge_label=1)
    before = service.get(actor, first)
    reset = service.reset(actor, first)
    assert reset.state is ReadinessState.UNCALIBRATED
    assert reset.generation == before.generation + 1
    assert service.get(actor, second).label_count == 0
    assert service.get(actor, second).generation == 0
    storage.close()
