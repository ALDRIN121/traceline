"""End-to-end acceptance for a calibrated judge becoming gate-eligible."""

from __future__ import annotations

from llm_agent_eval.auth import Actor
from llm_agent_eval.calibration import JudgeCalibrationService
from llm_agent_eval.contracts import InvocationResult
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.rubric_store import RubricContent, RubricStore
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.spec import JudgeBinding
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class _FakeTarget:
    def invoke(self, manifest, case_input, context):
        return InvocationResult(
            outcome="ok",
            output={"answer": "good"},
            capabilities={"final_output": "observed"},
        )


def test_calibrated_judge_is_gate_eligible_through_authorized_worker_run(tmp_path):
    storage = Storage(tmp_path / "calibrated-judge.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="calibrated-judge-agent")
    binding = JudgeBinding(
        provider="fixture", model="fixture-model", schema_version="1", rubric_version="1",
    )

    readiness = JudgeCalibrationService(storage).get(actor, binding)
    assert readiness.is_provisional
    for index in range(30):
        readiness = JudgeCalibrationService(storage).record_label(
            actor,
            binding,
            case_id=f"calibration-{index}",
            human_label="pass",
            judge_label="pass",
        )
    assert readiness.state.value == "CALIBRATED"
    assert readiness.is_provisional is False

    versions = VersionStore(storage)
    rubric = RubricStore(storage).create(
        project.project_id,
        RubricContent(
            rubric_id="quality",
            instructions="Score whether the response is useful.",
            candidate_fields=["answer"],
        ),
        0,
        actor,
    )
    evaluation = versions.create(
        "evaluation",
        project.project_id,
        {
            "spec": {
                "spec_version": "1",
                "name": "calibrated-judge-run",
                "dataset_version": "v1",
                "cases": [{"case_id": "c1", "name": "one", "input": {"q": "hi"}}],
                "metrics": [{
                    "metric_id": "quality",
                    "name": "quality",
                    "type": "judge",
                    "target": {"type": "input", "on_missing": "fail"},
                    "evaluator": {"type": "llm_judge", "rubric_version_id": rubric.version_id},
                    "scoring": {
                        "type": "binary", "range": [0, 1], "condition": "raw_value >= 0.8",
                    },
                    "aggregation": {"method": "pass_rate", "on_error": "fail"},
                    "judge_binding": binding.model_dump(),
                    "provisional": False,
                }],
            },
        },
        0,
        actor,
    )
    dataset = versions.create(
        "dataset",
        project.project_id,
        {"cases": [{"case_id": "c1", "name": "one", "input": {"q": "hi"}}]},
        0,
        actor,
    )
    gateway = MockGateway(
        {"chat_json": lambda messages, schema: {
            "score": 0.95,
            "justification": "good response",
            "evidence_refs": [],
        }},
        provider=binding.provider,
        model=binding.model,
    )
    worker = WorkflowWorker(
        storage,
        tmp_path / "artifacts",
        gateway,
        fingerprint_key=b"f" * 32,
        execution_target_factory=lambda actor, refs, context: (
            _FakeTarget(), {}, ("/source/agent",)
        ),
    )

    plans = RunPlanService(storage)
    plan = plans.plan_run(
        actor,
        {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        },
        {"tier": "quick"},
    )
    assert plan.state == "validated"
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    job = plans.enqueue_run(
        worker,
        actor,
        plan.plan_id,
        authorization.authorization_id,
        plan.content_digest,
        "calibrated-judge-once",
    )

    terminal = worker.service(actor).run_once(job_id=job.job_id)

    assert terminal.status == "completed"
    run = storage.get_run(terminal.result["run_id"], "ws")
    assert run is not None
    metric = storage.get_run_metric_results(run.run_id, "ws")[0]
    assert metric.aggregation_state == "COMPLETE"
    assert metric.value == 1.0
    assert metric.gate_status == "PASS"
    assert metric.no_ci is True
    assert gateway.calls and gateway.calls[0][0] == "chat_json"
