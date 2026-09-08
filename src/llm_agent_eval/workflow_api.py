"""Small durable-object API surface, registered by the existing app factory."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactStore
from .contracts import WorkflowError
from .versions import VersionStore
from .ingestion import ImportService


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


def workflow_router(store, artifact_root, max_artifact_bytes: int) -> APIRouter:
    router = APIRouter(prefix="/api")
    importer = ImportService(store(), artifact_root)

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

    @router.post("/import-jobs/{job_id}/drain")
    def drain_import_job(job_id: str, request: Request):
        """Worker dispatch boundary; leases exactly the requested durable job."""
        job = importer.drain(request.state.actor, job_id)
        return {"job_id": job.job_id, "status": job.status, "result": job.result, "error": job.error}

    router.import_service = importer  # test/worker wiring; not an HTTP capability

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
