from __future__ import annotations

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import InvocationResult
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.rubric_store import RubricContent, RubricStore
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class FakeTarget:
    def invoke(self, manifest, case_input, context):
        return InvocationResult(
            outcome="ok",
            output={"answer": "good"},
            capabilities={"final_output": "observed"},
        )


def test_authorized_worker_run_uses_persisted_rubric_and_gateway(tmp_path):
    storage = Storage(tmp_path / "judge.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="judge-agent")
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
                "name": "judge-run",
                "dataset_version": "v1",
                "cases": [{"case_id": "c1", "name": "one", "input": {"q": "hi"}}],
                "metrics": [{
                    "metric_id": "quality",
                    "name": "quality",
                    "type": "judge",
                    "target": {"type": "input", "on_missing": "fail"},
                    "evaluator": {"type": "llm_judge", "rubric_version_id": rubric.version_id},
                    "scoring": {"type": "binary", "range": [0, 1], "condition": "raw_value >= 0.8"},
                    "aggregation": {"method": "pass_rate", "on_error": "fail"},
                    "judge_binding": {
                        "provider": "fixture",
                        "model": "fixture-model",
                        "schema_version": "1",
                        "rubric_version": "1",
                    },
                    "provisional": True,
                }],
            }
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
        provider="fixture",
        model="fixture-model",
    )
    worker = WorkflowWorker(
        storage,
        tmp_path / "artifacts",
        gateway,
        fingerprint_key=b"f" * 32,
        execution_target_factory=lambda actor, refs, context: (FakeTarget(), {}, ("/source/agent",)),
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
    authorization = plans.authorize(actor, plan.plan_id, plan.content_digest)
    job = plans.enqueue_run(
        worker, actor, plan.plan_id, authorization.authorization_id,
        plan.content_digest, "judge-once",
    )

    terminal = worker.service(actor).run_once(job_id=job.job_id)

    assert terminal.status == "completed"
    assert gateway.calls and gateway.calls[0][0] == "chat_json"
    run = storage.get_run(terminal.result["run_id"], "ws")
    assert run is not None
    score = storage.get_case_scores(run_id=run.run_id, workspace_id="ws")[0]
    assert score.status == "PASS"
    assert score.evidence_event_ids == ()
    assert "Score whether the response is useful." in gateway.calls[0][1][1]["content"]
