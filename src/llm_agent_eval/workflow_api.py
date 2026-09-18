"""Small durable-object API surface, registered by the existing app factory."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactStore
from .contracts import WorkflowError
from .datasets import DatasetService
from .previews import PreviewService
from .run_plans import RunPlanService
from .knowledge import KnowledgeStore
from .sessions import SessionStore
from .targets import ConnectionService
from .versions import VersionStore
from .worker import WorkflowWorker


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    content: dict[str, Any]


class KnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    facts: list[dict[str, Any]]
    confirmed: bool = Field(strict=True)
    artifact_ids: list[str] = Field(default_factory=list)


class ConfirmKnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    report_id: str
    corrections: list[dict[str, Any]]


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    budget: int = Field(ge=1, le=20, strict=True)


class DatasetImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str
    mapping: dict[str, Any]
    metric_requirements: list[dict[str, Any]]
    dataset_id: str | None = None


class DashboardVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    definition: dict[str, Any]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_version_id: str
    dataset_version_id: str
    generator_version: str = "1"


class DatasetCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    explicit_exclusions: list[str] = Field(default_factory=list)
    expected_revision: int = Field(ge=0, strict=True)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    message: str | None = None
    card_action: dict[str, Any] | None = None


class RunPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_refs: dict[str, str]
    limits: dict[str, Any] = Field(default_factory=dict)


class RunAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str
    ttl_seconds: int = Field(default=600, ge=1, le=3600, strict=True)


class RunSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    plan_hash: str
    authorization_id: str


#: Kinds with dedicated, validated creation paths (ProfileStore, RubricStore);
#: the generic route must never publish attacker-shaped JSON for them.
PROTECTED_VERSION_KINDS = frozenset({"model_profile", "model_selection", "judge_rubric"})


def workflow_router(store, artifact_root, max_artifact_bytes: int, gateway) -> APIRouter:
    router = APIRouter(prefix="/api")
    from pathlib import Path
    from .install_state import load_fingerprint_key
    worker = WorkflowWorker(
        store(), artifact_root, gateway,
        fingerprint_key_source=lambda: load_fingerprint_key(Path(artifact_root) / "install-fingerprint.key"),
    )
    importer = worker.importer
    knowledge = KnowledgeStore(store(), artifact_root)
    datasets = DatasetService(store(), artifact_root)
    previews = PreviewService(store(), artifact_root)
    connections = worker.connections
    run_plans = RunPlanService(store())

    def artifacts(request):
        return ArtifactStore(store(), artifact_root, request.state.actor)

    @router.post("/uploads", status_code=201)
    async def upload_source(request: Request):
        """A scoped opaque upload ID; it is not an extracted source tree."""
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > max_artifact_bytes:
                raise WorkflowError("Upload exceeds the archive limit", code="quota_exceeded", status=413)
            data.extend(chunk)
        upload_id = importer.put_upload(request.state.actor, bytes(data))
        return {"state": "uploaded", "upload_id": upload_id}

    @router.post("/projects/{project_id}/imports", status_code=202)
    def import_source(project_id: str, body: dict[str, Any], request: Request):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise WorkflowError("An Idempotency-Key is required")
        job = importer.submit(request.state.actor, project_id, body, key)
        return {"state": "queued", "job_id": job.job_id}

    @router.get("/import-jobs/{job_id}")
    def get_import_job(job_id: str, request: Request):
        job = importer._queue(request.state.actor).get(job_id)
        return {"job_id": job.job_id, "status": job.status, "result": job.result, "error": job.error}

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        """Read one workspace-scoped workflow job without exposing its command."""
        job = worker.queue(request.state.actor).get(job_id)
        return {"job_id": job.job_id, "status": job.status, "result": job.result, "error": job.error}

    @router.post("/import-jobs/{job_id}/drain")
    def drain_import_job(job_id: str, request: Request):
        """Worker dispatch boundary; leases exactly the requested durable job."""
        job = worker.drain(request.state.actor, job_id)
        return {"job_id": job.job_id, "status": job.status, "result": job.result, "error": job.error}

    router.import_service = importer
    router.worker = worker

    @router.post("/run-plans", status_code=201)
    def create_run_plan(body: RunPlanRequest, request: Request):
        plan = run_plans.plan_run(request.state.actor, body.version_refs, body.limits)
        return {"state": plan.state, "plan": asdict(plan)}

    @router.get("/run-plans/{plan_id}")
    def get_run_plan(plan_id: str, request: Request):
        plan = run_plans.get_plan(request.state.actor, plan_id)
        return {"state": plan.state, "plan": asdict(plan)}

    @router.post("/run-plans/{plan_id}/authorize", status_code=201)
    def authorize_run_plan(plan_id: str, body: RunAuthorizationRequest, request: Request):
        authorization = run_plans.authorize(
            request.state.actor, plan_id, body.plan_hash, ttl_seconds=body.ttl_seconds,
        )
        return {"state": authorization.state, "authorization": asdict(authorization)}

    @router.post("/runs", status_code=202)
    def enqueue_run(body: RunSubmissionRequest, request: Request):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise WorkflowError("An Idempotency-Key is required")
        job = run_plans.enqueue_run(
            worker, request.state.actor, body.plan_id, body.authorization_id,
            body.plan_hash, key,
        )
        return {"state": "queued", "job_id": job.job_id, "plan_id": body.plan_id}

    @router.post("/projects/{project_id}/sessions", status_code=201)
    def create_session(project_id: str, body: dict[str, Any], request: Request):
        evaluation_id = body.get("evaluation_id")
        if not isinstance(evaluation_id, str):
            raise WorkflowError("evaluation_id is required")
        session = SessionStore(store()).create(
            request.state.actor, project_id, evaluation_id,
            evaluation_version_id=body.get("evaluation_version_id"),
            knowledge_version_id=body.get("knowledge_version_id"),
        )
        return {"state": "draft", **session}

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str, request: Request):
        return SessionStore(store()).get(request.state.actor, session_id)

    @router.post("/sessions/{session_id}/turns", status_code=202)
    def session_turn(session_id: str, body: TurnRequest, request: Request):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise WorkflowError("An Idempotency-Key is required")
        job = worker.submit_turn(request.state.actor, session_id, body.model_dump(), key)
        return {"state": "queued", "job_id": job.job_id}

    @router.get("/projects/{project_id}/knowledge")
    def get_knowledge(project_id: str, request: Request):
        return knowledge.current(project_id, request.state.actor)

    @router.post("/projects/{project_id}/knowledge/confirm", status_code=201)
    def confirm_knowledge(project_id: str, body: ConfirmKnowledgeRequest, request: Request):
        return knowledge.confirm(
            project_id, body.report_id, body.corrections, body.expected_revision, request.state.actor,
        )

    @router.post("/projects/{project_id}/knowledge/retrieve")
    def retrieve_knowledge(project_id: str, body: RetrieveRequest, request: Request):
        return knowledge.retrieve(project_id, body.query, body.budget, request.state.actor)

    @router.post("/projects/{project_id}/datasets/imports", status_code=201)
    def import_dataset(project_id: str, body: DatasetImportRequest, request: Request):
        return datasets.validate_import(
            request.state.actor, project_id, body.upload_id, body.mapping, body.metric_requirements,
            dataset_id=body.dataset_id,
        )

    @router.post("/datasets/{dataset_id}/commit", status_code=201)
    def commit_dataset(dataset_id: str, body: DatasetCommitRequest, request: Request):
        return datasets.commit_dataset(
            request.state.actor, dataset_id, body.report_id, body.explicit_exclusions, body.expected_revision,
        )

    @router.get("/datasets/{dataset_id}/versions/{version_id}")
    def get_dataset_version(dataset_id: str, version_id: str, request: Request):
        return datasets.get_version(request.state.actor, dataset_id, version_id)

    @router.post("/dashboards/{dashboard_id}/versions", status_code=201)
    def create_dashboard_version(dashboard_id: str, body: DashboardVersionRequest, request: Request):
        return previews.create_version(
            request.state.actor, dashboard_id, body.definition, body.expected_revision,
        )

    @router.post("/dashboard-versions/{version_id}/preview", status_code=202)
    def preview_dashboard(version_id: str, body: PreviewRequest, request: Request):
        import uuid
        key = request.headers.get("Idempotency-Key") or uuid.uuid4().hex
        job = worker.submit_preview(request.state.actor, version_id, body.model_dump(), key)
        return {"state": "queued", "job_id": job.job_id}

    @router.post("/projects/{project_id}/connections", status_code=201)
    def create_connection(project_id: str, body: dict[str, Any], request: Request):
        return connections.create(request.state.actor, project_id, body)

    @router.post("/projects/{project_id}/openapi-imports", status_code=201)
    def import_openapi(project_id: str, body: dict[str, Any], request: Request):
        return connections.import_openapi(request.state.actor, project_id, body)

    @router.post("/targets/{target_id}/verify", status_code=202)
    def verify_target(target_id: str, body: dict[str, Any], request: Request):
        import uuid as uuid_mod
        key = request.headers.get("Idempotency-Key") or uuid_mod.uuid4().hex
        job = worker.submit_verify(request.state.actor, target_id, body, key)
        return {"state": "queued", "job_id": job.job_id}

    @router.post("/projects/{project_id}/knowledge/versions", status_code=201)
    def create_knowledge(project_id: str, body: KnowledgeRequest, request: Request):
        version = VersionStore(store()).create("knowledge", project_id,
            body.model_dump(exclude={"expected_revision"}), body.expected_revision, request.state.actor)
        return {"state": "draft", "version": asdict(version)}

    @router.get("/projects/{project_id}/knowledge/versions")
    def list_knowledge(project_id: str, request: Request):
        return VersionStore(store()).list("knowledge", project_id, request.state.actor)

    @router.post("/objects/{kind}/{parent_id}/versions", status_code=201)
    def create_version(kind: str, parent_id: str, body: VersionRequest, request: Request):
        if kind in PROTECTED_VERSION_KINDS:
            raise WorkflowError(
                "This version kind requires its dedicated validated creation path",
                code="protected_version_kind",
            )
        version = VersionStore(store()).create(kind, parent_id, body.content, body.expected_revision, request.state.actor)
        return {"state": "draft", "version": asdict(version)}

    @router.get("/objects/{kind}/{parent_id}/versions")
    def list_versions(kind: str, parent_id: str, request: Request):
        return VersionStore(store()).list(kind, parent_id, request.state.actor)

    @router.get("/versions/{version_id}")
    def get_version(version_id: str, request: Request):
        return {"version": asdict(VersionStore(store()).get(version_id, request.state.actor))}

    @router.post("/definitions/migrate")
    def migrate_legacy(request: Request):
        return {"definitions": VersionStore(store()).migrate_legacy(request.state.actor)}

    @router.post("/artifacts", status_code=201)
    async def put_artifact(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > max_artifact_bytes:
                raise WorkflowError("Artifact exceeds the upload limit", code="quota_exceeded", status=413)
            data.extend(chunk)
        try:
            record = artifacts(request).put(request.state.actor.workspace_id, bytes(data),
                request.headers.get("content-type", "application/octet-stream").split(";", 1)[0])
        except OSError as exc:
            raise WorkflowError("Artifact could not be stored", code="internal_error", status=500) from exc
        return {"state": "ready", "artifact": asdict(record)}

    @router.get("/artifacts")
    def list_artifacts(request: Request):
        return {"artifacts": [asdict(record) for record in artifacts(request).list(request.state.actor.workspace_id)]}

    @router.get("/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str, request: Request):
        service, ws = artifacts(request), request.state.actor.workspace_id
        data = service.get(ws, artifact_id)
        record = service.record(ws, artifact_id)
        return Response(data, media_type=record.media_type, headers={
            "Content-Disposition": f'attachment; filename="{record.artifact_id}"',
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "ETag": f'"{record.checksum}"',
        })

    return router
