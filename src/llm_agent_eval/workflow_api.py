"""Small durable-object API surface, registered by the existing app factory."""

from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .artifacts import ArtifactStore
from .calibration import JudgeCalibrationService
from .ci_api import exit_code as ci_exit_code
from .comparisons import ComparisonService
from .contracts import NotFound, WorkflowError
from .egress.recording import Budget
from .egress.routes import ProviderRouteRegistry
from .egress.session import ProxyRunSession
from .evaluator_registry import CustomEvaluatorRegistry
from .datasets import DatasetService
from .previews import PreviewService
from .profiles import ModelProfile, ProfileStore, require_admin
from .rubric_store import RubricContent, RubricStore
from .schedules import DurableScheduleService
from .run_plans import RunPlanService
from .knowledge import KnowledgeStore
from .sessions import SessionStore
from .spec import JudgeBinding
from .targets import ConnectionService
from .versions import VersionStore
from .worker import WorkflowWorker
from .secrets import SecretStore


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    content: dict[str, Any]


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    entrypoint: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("project name is required")
        return value

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_is_bounded(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 1024 for item in value):
            raise ValueError("entrypoint items must be non-empty and bounded")
        return value


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


class EvaluatorVersionRequest(BaseModel):
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


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    plan_hash: str
    authorization_id: str
    timezone: str
    local_time: str
    dst_policy: str = "first"
    version_policy: str = "frozen"
    daily_request_limit: int = Field(default=1, ge=1, le=10_000, strict=True)
    daily_budget_usd_micros: int = Field(default=0, ge=0, strict=True)


class ScheduleSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str
    owner: str = Field(min_length=1, max_length=255)


class PrepareSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_profile: dict[str, Any]


class ModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    profile: ModelProfile


class ModelSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    harness_profile_id: str
    judge_profile_id: str


class JudgeRubricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    rubric: RubricContent


class JudgeCalibrationBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: JudgeBinding


class JudgeCalibrationLabelRequest(JudgeCalibrationBindingRequest):
    case_id: str = Field(min_length=1, max_length=255)
    human_label: Any
    judge_label: Any
    label_id: str | None = Field(default=None, min_length=1, max_length=255)


class SecretCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret_type: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    value: str = Field(min_length=1, max_length=16_384)
    allowed_services: list[str] = Field(min_length=1, max_length=4)


class SecretRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=16_384)
    allowed_services: list[str] = Field(min_length=1, max_length=4)


#: Kinds with dedicated, validated creation paths (ProfileStore, RubricStore);
#: the generic route must never publish attacker-shaped JSON for them.
PROTECTED_VERSION_KINDS = frozenset({"evaluator", "model_profile", "model_selection", "judge_rubric"})
SUPPORTED_SECRET_SERVICES = frozenset({"proxy"})


def _validate_secret_services(services: list[str]) -> set[str]:
    normalized = {item.strip() for item in services if isinstance(item, str) and item.strip()}
    if len(normalized) != len(services) or not normalized or not normalized <= SUPPORTED_SECRET_SERVICES:
        raise WorkflowError(
            "allowed_services contains an unsupported service",
            code="secret_service_invalid", status=422,
        )
    return normalized


def _readiness_payload(readiness) -> dict[str, Any]:
    return {
        "state": readiness.state.value,
        "label_count": readiness.label_count,
        "kappa": readiness.kappa,
        "min_labels": readiness.min_labels,
        "kappa_ready": readiness.kappa_ready,
        "kappa_reset": readiness.kappa_reset,
        "generation": readiness.generation,
        "provisional": readiness.is_provisional,
    }


def default_proxy_session_factory(store, artifact_root):
    """Build trusted per-run proxy sessions from install-owned configuration.

    Route metadata is loaded only when a proxy run is dispatched. Missing or
    unsafe configuration therefore fails the run closed, while ordinary
    hosted/API workflows remain usable without local-provider setup.
    """
    from pathlib import Path

    root = Path(artifact_root)
    route_path = root / "proxy-routes.json"
    install_root = root / "install"
    secret_key = root / "install-secret.key"
    store_provider = store if callable(store) else lambda: store

    def factory(actor, run_id, plan, _context):
        routes = ProviderRouteRegistry(route_path).load()
        limits = plan.content.get("limits") or {}
        budget_usd_micros = limits.get("budget_usd_micros", 0)
        if type(budget_usd_micros) is not int or budget_usd_micros < 0:
            raise WorkflowError(
                "proxy run budget is invalid", code="proxy_budget_invalid", status=409,
            )
        secrets = SecretStore(store_provider(), actor, secret_key)
        return ProxyRunSession(
            run_id=run_id,
            install_root=install_root,
            routes=routes,
            budget=Budget(budget_usd_micros),
            secret_resolver=secrets.resolve,
            record=lambda _record: None,
        )

    return factory


# Keep the old private name for integrations that imported it while the
# worker-facing factory became a shared production wiring point.
_default_proxy_session_factory = default_proxy_session_factory


def workflow_router(store, artifact_root, max_artifact_bytes: int, gateway) -> APIRouter:
    router = APIRouter(prefix="/api")
    from pathlib import Path
    from .install_state import load_fingerprint_key
    worker = WorkflowWorker(
        store(), artifact_root, gateway,
        fingerprint_key_source=lambda: load_fingerprint_key(Path(artifact_root) / "install-fingerprint.key"),
        proxy_session_factory=default_proxy_session_factory(store, artifact_root),
    )
    importer = worker.importer
    knowledge = KnowledgeStore(store(), artifact_root)
    datasets = DatasetService(store(), artifact_root)
    previews = PreviewService(store(), artifact_root)
    connections = worker.connections
    run_plans = RunPlanService(store())

    def artifacts(request):
        return ArtifactStore(store(), artifact_root, request.state.actor)

    @router.post("/projects", status_code=201)
    def create_project(body: ProjectCreateRequest, request: Request):
        actor = request.state.actor
        actor.require(write=True)
        project = store().create_project(
            workspace_id=actor.workspace_id,
            name=body.name,
            entrypoint=tuple(body.entrypoint),
        )
        return {"state": "created", "project": asdict(project)}

    @router.get("/projects")
    def list_projects(request: Request):
        actor = request.state.actor
        return {"projects": [asdict(project) for project in store().list_projects(actor.workspace_id)]}

    @router.get("/projects/{project_id}")
    def get_project(project_id: str, request: Request):
        actor = request.state.actor
        project = store().get_project(project_id, actor.workspace_id)
        if project is None:
            raise NotFound()
        return {"project": asdict(project)}

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

    @router.post("/source-versions/{version_id}/prepare", status_code=202)
    def prepare_source(version_id: str, body: PrepareSourceRequest, request: Request):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise WorkflowError("An Idempotency-Key is required")
        job = worker.queue(request.state.actor).enqueue({
            "kind": "source_prepare", "source_version_id": version_id,
            "runtime_profile": body.runtime_profile,
        }, key)
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

    def _enqueue_plan_run(body: RunSubmissionRequest, request: Request, key: str):
        job = run_plans.enqueue_run(
            worker, request.state.actor, body.plan_id, body.authorization_id,
            body.plan_hash, key,
        )
        return {"state": "queued", "job_id": job.job_id, "plan_id": body.plan_id}

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
        return _enqueue_plan_run(body, request, key)

    @router.post("/ci/runs", status_code=202)
    def enqueue_ci_run(body: RunSubmissionRequest, request: Request):
        """Authenticated CI entrypoint over the same immutable run-plan path."""
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise WorkflowError("An Idempotency-Key is required")
        return _enqueue_plan_run(body, request, key)

    @router.post("/webhooks/evaluations", status_code=202)
    def enqueue_webhook_run(body: RunSubmissionRequest, request: Request):
        """Trigger a pre-authorized evaluation from a signed/authenticated webhook.

        The deployment authentication boundary authenticates the webhook request;
        the delivery ID becomes the durable idempotency key so provider retries
        cannot enqueue or spend twice.
        """
        delivery = request.headers.get("X-Webhook-Delivery")
        key = request.headers.get("Idempotency-Key") or delivery
        if not key:
            raise WorkflowError("X-Webhook-Delivery or Idempotency-Key is required")
        return {
            **_enqueue_plan_run(body, request, f"webhook:{key}"),
            "trigger": "webhook",
        }

    @router.get("/ci/runs/{run_id}")
    def get_ci_run(run_id: str, request: Request):
        run = store().get_run(run_id, request.state.actor.workspace_id)
        if run is None:
            raise WorkflowError("Run not found", code="not_found", status=404)
        metrics = [
            {
                "metric_id": metric.metric_id,
                "value": metric.value,
                "method": metric.method,
                "aggregation_state": metric.aggregation_state,
                "gate_status": metric.gate_status,
                "no_ci": metric.no_ci,
                "sample_n": metric.sample_n,
                "error_n": metric.error_n,
                "skipped_n": metric.skipped_n,
                "computed_at": metric.computed_at,
            }
            for metric in store().get_run_metric_results(run_id, request.state.actor.workspace_id)
        ]
        payload = {"run_id": run_id, "status": run.status, "metrics": metrics}
        return {**payload, "exit_code": ci_exit_code(payload)}

    @router.post("/comparisons")
    def compare_ci_runs(body: dict[str, Any], request: Request):
        return ComparisonService(store()).compare(
            request.state.actor.workspace_id,
            body.get("baseline_run_id"), body.get("candidate_run_id"),
            body.get("metric_id"), resamples=body.get("resamples", 10_000),
            seed=body.get("seed", 0),
        )

    @router.post("/schedules", status_code=201)
    def create_schedule(body: ScheduleRequest, request: Request):
        schedule = DurableScheduleService(store()).create(
            request.state.actor,
            plan_id=body.plan_id,
            plan_hash=body.plan_hash,
            authorization_id=body.authorization_id,
            timezone_name=body.timezone,
            local_time=body.local_time,
            dst_policy=body.dst_policy,
            version_policy=body.version_policy,
            daily_request_limit=body.daily_request_limit,
            daily_budget_usd_micros=body.daily_budget_usd_micros,
        )
        return {"state": schedule.state, "schedule": asdict(schedule)}

    @router.get("/schedules")
    def list_schedules(request: Request):
        schedules = DurableScheduleService(store()).storage.list_schedules(
            request.state.actor.workspace_id,
        )
        return {"schedules": [asdict(schedule) for schedule in schedules]}

    @router.get("/schedules/{schedule_id}")
    def get_schedule(schedule_id: str, request: Request):
        schedule = store().get_schedule(schedule_id, request.state.actor.workspace_id)
        if schedule is None:
            raise WorkflowError("Schedule not found", code="not_found", status=404)
        slots = store().list_schedule_slots(schedule_id, request.state.actor.workspace_id)
        return {"schedule": asdict(schedule), "slots": [asdict(slot) for slot in slots]}

    @router.post("/schedules/{schedule_id}/pause")
    def pause_schedule(schedule_id: str, request: Request):
        schedule = DurableScheduleService(store()).set_state(
            request.state.actor, schedule_id, "paused",
        )
        return {"state": schedule.state, "schedule": asdict(schedule)}

    @router.post("/schedules/{schedule_id}/resume")
    def resume_schedule(schedule_id: str, request: Request):
        schedule = DurableScheduleService(store()).set_state(
            request.state.actor, schedule_id, "active",
        )
        return {"state": schedule.state, "schedule": asdict(schedule)}

    @router.post("/schedules/{schedule_id}/sweep", status_code=202)
    def sweep_schedule(schedule_id: str, body: ScheduleSweepRequest, request: Request):
        try:
            start, end = date.fromisoformat(body.start), date.fromisoformat(body.end)
        except ValueError:
            raise WorkflowError("start and end must be ISO dates", code="schedule_invalid") from None
        jobs = worker.sweep_schedules(
            request.state.actor, schedule_id, start, end, owner=body.owner,
        )
        return {"state": "queued", "jobs": [job.job_id for job in jobs]}

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

    @router.post("/projects/{project_id}/model-profiles", status_code=201)
    def create_model_profile(project_id: str, body: ModelProfileRequest, request: Request):
        version = ProfileStore(store()).create(
            project_id, body.profile, body.expected_revision, request.state.actor,
        )
        return {"state": "draft", "version": asdict(version)}

    @router.post("/projects/{project_id}/model-selection", status_code=201)
    def create_model_selection(project_id: str, body: ModelSelectionRequest, request: Request):
        version = ProfileStore(store()).select(
            project_id,
            harness_profile_id=body.harness_profile_id,
            judge_profile_id=body.judge_profile_id,
            expected_revision=body.expected_revision,
            actor=request.state.actor,
        )
        return {"state": "draft", "version": asdict(version)}

    @router.post("/projects/{project_id}/judge-rubrics", status_code=201)
    def create_judge_rubric(project_id: str, body: JudgeRubricRequest, request: Request):
        version = RubricStore(store()).create(
            project_id, body.rubric, body.expected_revision, request.state.actor,
        )
        return {"state": "draft", "version": asdict(version)}

    @router.post("/secrets", status_code=201)
    def create_secret(body: SecretCreateRequest, request: Request):
        require_admin(request.state.actor)
        allowed_services = _validate_secret_services(body.allowed_services)
        secret = SecretStore(
            store(), request.state.actor, Path(artifact_root) / "install-secret.key",
        ).put(body.secret_type, body.value, allowed_services=allowed_services)
        return {
            "state": "active",
            "secret": {
                "secret_id": secret.secret_id,
                "secret_type": secret.secret_type,
                "allowed_services": sorted(secret.allowed_services),
            },
        }

    @router.post("/secrets/{secret_id}/rotate")
    def rotate_secret(secret_id: str, body: SecretRotateRequest, request: Request):
        require_admin(request.state.actor)
        allowed_services = _validate_secret_services(body.allowed_services)
        secret = SecretStore(
            store(), request.state.actor, Path(artifact_root) / "install-secret.key",
        ).rotate(secret_id, body.value, allowed_services=allowed_services)
        return {
            "state": "active",
            "secret": {
                "secret_id": secret.secret_id,
                "allowed_services": sorted(secret.allowed_services),
            },
        }

    @router.post("/judge-calibration/labels", status_code=201)
    def add_judge_calibration_label(body: JudgeCalibrationLabelRequest, request: Request):
        readiness = JudgeCalibrationService(store()).record_label(
            request.state.actor, body.binding, case_id=body.case_id,
            human_label=body.human_label, judge_label=body.judge_label,
            label_id=body.label_id,
        )
        return {
            "state": "recorded", "binding": body.binding.model_dump(),
            "readiness": _readiness_payload(readiness),
        }

    @router.get("/judge-calibration")
    def get_judge_calibration(
        request: Request, provider: str, model: str,
        schema_version: str, rubric_version: str,
    ):
        binding = JudgeBinding(
            provider=provider, model=model,
            schema_version=schema_version, rubric_version=rubric_version,
        )
        readiness = JudgeCalibrationService(store()).get(request.state.actor, binding)
        return {"binding": binding.model_dump(), "readiness": _readiness_payload(readiness)}

    @router.post("/judge-calibration/reset", status_code=200)
    def reset_judge_calibration(body: JudgeCalibrationBindingRequest, request: Request):
        readiness = JudgeCalibrationService(store()).reset(request.state.actor, body.binding)
        return {
            "state": "reset", "binding": body.binding.model_dump(),
            "readiness": _readiness_payload(readiness),
        }

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

    @router.post("/projects/{project_id}/evaluator-versions", status_code=201)
    def create_evaluator_version(project_id: str, body: EvaluatorVersionRequest, request: Request):
        version = CustomEvaluatorRegistry(
            store(), artifact_root, request.state.actor,
        ).create(project_id, body.definition, body.expected_revision)
        return {"state": "draft", "version": asdict(version)}

    @router.get("/projects/{project_id}/evaluator-versions")
    def list_evaluator_versions(project_id: str, request: Request):
        return VersionStore(store()).list("evaluator", project_id, request.state.actor)

    @router.post("/evaluator-versions/{version_id}/approve", status_code=201)
    def approve_evaluator_version(version_id: str, request: Request):
        version = CustomEvaluatorRegistry(
            store(), artifact_root, request.state.actor,
        ).approve(version_id)
        return {"state": "approved", "version": asdict(version)}

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
