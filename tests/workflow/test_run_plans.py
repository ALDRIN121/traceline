from __future__ import annotations

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


def _service(tmp_path):
    storage = Storage(tmp_path / "plans.db")
    storage.create_schema()
    project = storage.create_project(workspace_id="ws-a", name="agent")
    actor = Actor("owner-a", "ws-a", "owner")
    versions = VersionStore(storage)
    evaluation = versions.create(
        "evaluation", project.project_id,
        {"spec": {"name": "eval", "cases": ["c1"]}}, 0, actor,
    )
    dataset = versions.create(
        "dataset", project.project_id,
        {"cases": [{"case_id": "c1", "input": {"q": "hi"}}]}, 0, actor,
    )
    return storage, actor, project, evaluation, dataset


def test_plan_freezes_authorized_version_refs_and_digest(tmp_path):
    storage, actor, project, evaluation, dataset = _service(tmp_path)
    try:
        service = RunPlanService(storage)
        plan = service.plan_run(
            actor,
            {
                "project_id": project.project_id,
                "evaluation_version_id": evaluation.version_id,
                "dataset_version_id": dataset.version_id,
            },
            {"tier": "quick", "repeats": 1, "budget_usd_micros": 1000},
        )

        assert plan.state == "validated"
        assert plan.content["version_refs"]["evaluation_version_id"] == evaluation.version_id
        assert plan.content["version_refs"]["dataset_version_id"] == dataset.version_id
        assert len(plan.content_digest) == 64

        authorization = service.authorize(actor, plan.plan_id, plan.content_digest)
        assert authorization.state == "authorized"
        assert authorization.plan_hash == plan.content_digest
    finally:
        storage.close()


def test_plan_reports_readiness_blockers_instead_of_authorizing_partial_refs(tmp_path):
    storage, actor, project, _evaluation, _dataset = _service(tmp_path)
    try:
        service = RunPlanService(storage)
        plan = service.plan_run(actor, {"project_id": project.project_id}, {})

        assert plan.state == "blocked"
        assert "evaluation_version_required" in plan.blockers
        assert "dataset_version_required" in plan.blockers
        with pytest.raises(WorkflowError) as error:
            service.authorize(actor, plan.plan_id, plan.content_digest)
        assert error.value.code == "run_plan_not_ready"
    finally:
        storage.close()


def test_authorization_rejects_a_stale_plan_hash(tmp_path):
    storage, actor, project, evaluation, dataset = _service(tmp_path)
    try:
        service = RunPlanService(storage)
        plan = service.plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        }, {})

        with pytest.raises(WorkflowError) as error:
            service.authorize(actor, plan.plan_id, "0" * 64)
        assert error.value.code == "authorization_hash_mismatch"
    finally:
        storage.close()


def test_evaluation_run_submission_is_idempotent_and_workspace_scoped(tmp_path):
    storage, actor, project, evaluation, dataset = _service(tmp_path)
    worker = WorkflowWorker(
        storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
    )
    try:
        service = RunPlanService(storage)
        plan = service.plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        }, {})
        authorization = service.authorize(actor, plan.plan_id, plan.content_digest)

        first = service.enqueue_run(worker, actor, plan.plan_id, authorization.authorization_id,
                                    plan.content_digest, "run-once")
        second = service.enqueue_run(worker, actor, plan.plan_id, authorization.authorization_id,
                                     plan.content_digest, "run-once")
        assert first.job_id == second.job_id
        assert first.command["kind"] == "evaluation_run"
        assert first.command["plan_hash"] == plan.content_digest

        with pytest.raises(WorkflowError) as error:
            service.get_plan(Actor("other", "ws-b", "owner"), plan.plan_id)
        assert error.value.code == "not_found"
    finally:
        storage.close()
