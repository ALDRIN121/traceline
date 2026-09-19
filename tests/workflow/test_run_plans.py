from __future__ import annotations

import uuid
import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.artifacts import ArtifactStore
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


def test_plan_freezes_only_approved_custom_evaluator_bindings(tmp_path):
    storage, actor, project, base_evaluation, dataset = _service(tmp_path)
    try:
        import io
        import zipfile

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("evaluate.py", "# evaluator")
        artifact = ArtifactStore(storage, tmp_path / "artifacts", actor).put(
            "ws-a", archive.getvalue(), "application/zip"
        )
        from llm_agent_eval.evaluator_registry import CustomEvaluatorRegistry
        registry = CustomEvaluatorRegistry(storage, tmp_path / "artifacts", actor)
        draft = registry.create(project.project_id, {
            "name": "policy", "artifact_ids": [artifact.artifact_id],
            "image_digest": "sha256:" + "a" * 64,
            "entrypoint": ["/usr/bin/python", "/source/evaluate.py"],
            "score_range": [0, 1], "pass_threshold": 0.5,
        }, 0)
        evaluation = VersionStore(storage).create("evaluation", project.project_id, {
            "spec": {"metrics": [{"metric_id": "policy", "scoring": {"range": [0, 1]}}]},
            "custom_evaluators": {"policy": draft.version_id},
        }, base_evaluation.revision, actor)
        blocked = RunPlanService(storage).plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        }, {})
        assert blocked.state == "blocked"
        assert "custom_evaluator_approval_required:policy" in blocked.blockers

        approved = registry.approve(draft.version_id)
        evaluation = VersionStore(storage).create("evaluation", project.project_id, {
            "spec": {"metrics": [{"metric_id": "policy", "scoring": {"range": [0, 1]}}]},
            "custom_evaluators": {"policy": approved.version_id},
        }, evaluation.revision, actor)
        plan = RunPlanService(storage).plan_run(actor, {
            "project_id": project.project_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
        }, {})
        assert plan.state == "validated"
        assert plan.content["evaluator_versions"] == {"policy": approved.version_id}
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


def test_plan_rejects_metrics_that_target_cannot_evidence(tmp_path):
    storage, actor, project, _evaluation, dataset = _service(tmp_path)
    isolated_project = storage.create_project(workspace_id="ws-a", name="capability-test")
    target_id = uuid.uuid4().hex
    with storage.workspace_transaction(actor.workspace_id) as conn:
        conn.execute(
            "INSERT INTO targets (workspace_id,target_id,project_id,created_at) VALUES (?,?,?,?)",
            (actor.workspace_id, target_id, isolated_project.project_id, "2026-01-01T00:00:00+00:00"),
        )
    versions = VersionStore(storage)
    target = versions.create(
        "target", target_id,
        {
            "kind": "http_json",
            "verification": {
                "state": "verified",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "capabilities": {
                    "final_output": "observed",
                    "tool_execution": "unavailable",
                    "retrieval": "unavailable",
                },
            },
        }, 0, actor,
    )
    unsupported = versions.create(
        "evaluation", isolated_project.project_id,
        {"spec": {"metrics": [
            {"metric_id": "tools", "target": {"type": "tool_output"}},
            {"metric_id": "retrieval", "target": {"type": "retrieval"}},
        ]}}, 0, actor,
    )
    try:
        plan = RunPlanService(storage).plan_run(actor, {
            "project_id": isolated_project.project_id,
            "evaluation_version_id": unsupported.version_id,
            "dataset_version_id": dataset.version_id,
            "target_version_id": target.version_id,
        }, {})
        assert plan.state == "blocked"
        assert "target_capability_required:tools:tool_execution" in plan.blockers
        assert "target_capability_required:retrieval:retrieval" in plan.blockers
    finally:
        storage.close()
