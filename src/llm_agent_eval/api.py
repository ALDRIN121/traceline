"""The FastAPI application — the interaction surface (harness §17).

Implements §17.4's run APIs (create / start / cancel / status / progress /
traces / revisions), §17.6 idempotent creation, §17.7 cancellation as a
request never a promise, §17.8 SSE progress, §17.9 cursor pagination, and
§17.10's uniform error envelope.

Execution model: the engine is sync and single-threaded, so runs execute in
background threads — never a new event loop. Every store access is
serialized by the storage connection lock (storage.py); the worker registry
is guarded by its own lock. The engine's run loop polls a per-run cancel
event between cases (§17.7: the in-flight case is allowed to finish; the
signal is honored at the case boundary).

Two trace streams are never conflated: this surface exposes agent-under-test
traces (``trace_events``) only — harness sessions are a separate surface.

Score revisions (§13A.1): overrides are stored ALONGSIDE the machine score as
``score_revisions`` rows (additive, idempotent by PK, diffable, annotated);
the machine row is never modified — only flagged ``overridden`` for ordering.

The default application is created lazily (via ``__getattr__``) so importing
this module never opens a database file — ``create_app`` is the constructor;
``app`` is the conventional uvicorn target.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .auth import Actor
from .contracts import WorkflowError
from .gateway import LiteLLMGateway, ModelGateway
from .dashboard import (
    DEFAULT_DEFINITION,
    get_registry_version,
    registry_components,
    resolve_definition,
)
from .engine import (
    Engine,
    EngineError,
    ProjectNotEvaluableError,
    RunAlreadyTerminalError,
    TierViolationError,
)
from .lifecycle import IllegalTransition, RunCaseStatus, RunStatus, is_run_terminal
from .spec import SpecValidationError, validate_spec
from .storage import CustomEvalRecord, Storage

logger = logging.getLogger(__name__)

#: Run statuses that terminate a run and an SSE stream. ``incomplete`` is the
#: engine's §11B.8 marker — never a final state, but the stream ends for it.
_TERMINAL_RUN_STATUSES = ("complete", "failed", "cancelled", "incomplete")


class CreateRunRequest(BaseModel):
    """POST /runs body (§17.4). ``spec`` is the evaluation spec (JSON object
    or text); ``tier`` defaults to the spec's ``run_tier`` hint. Runs created
    without a ``project_id`` take their ``entrypoint`` directly."""

    spec: dict[str, Any] | str
    tier: str | None = None
    idempotency_key: str | None = None
    project_id: str | None = None
    entrypoint: list[str] | None = None
    repeats: int = 1
    retry_max: int = 0
    source_digest: str | None = None
    cwd: str | None = None
    timeout_seconds: float = 120.0
    budget_usd_micros: int = 0
    concurrency: int = 1
    created_by: str | None = None


class RevisionRequest(BaseModel):
    """POST /runs/{id}/metrics/{metric_id}/revisions body (§13A.1)."""

    case_id: str
    score_revision: int = Field(ge=1)
    override_value: Any = None
    dispute_path: Literal["rule_wrong", "evidence_missing", "judgment_wrong"] | None = None
    reason: str | None = None
    author_id: str | None = None


class ValidateSpecRequest(BaseModel):
    """POST /api/evaluations/validate body."""

    spec: dict[str, Any] | str


class AuthorSpecRequest(BaseModel):
    """POST /api/harness/author body."""

    intent: str
    repair_attempts: int | None = None

#: §17.10 error taxonomy → HTTP status mapping.
_ERROR_STATUS = {
    "validation_error": 422,
    "not_found": 404,
    "conflict": 409,
    "state_conflict": 409,
    "project_not_evaluable": 409,
    "idempotency_conflict": 409,
    "quota_exceeded": 429,
    "provider_unreachable": 502,
    "internal_error": 500,
}


# ---------------------------------------------------------------------------
# Serialization helpers (models are pydantic/records; responses are plain
# dicts so the wire format is explicit and stable)
# ---------------------------------------------------------------------------


def _run_summary(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "status": run.status,
        "tier": run.tier,
        "case_count": run.case_count,
        "concurrency": run.concurrency,
        "repeats": run.repeats,
        "retry_max": run.retry_max,
        "created_at": run.created_at,
        "run_started_at": run.run_started_at,
        "run_completed_at": run.run_completed_at,
        "idempotency_key": run.idempotency_key,
        "spec": {
            "spec_version": run.spec.spec_version,
            "name": run.spec.name,
            "dataset_version": run.spec.dataset_version,
            "metrics": [
                {
                    "metric_id": m.metric_id,
                    "name": m.name,
                    "type": m.type,
                    "provisional": m.provisional,
                    "judge_binding": m.judge_binding.model_dump() if m.judge_binding else None,
                }
                for m in run.spec.metrics
            ],
        },
    }


def _case_metric_dict(r: Any) -> dict[str, Any]:
    return {
        "metric_id": r.metric_id,
        "case_id": r.case_id,
        "score_revision": r.score_revision,
        "status": r.status,
        "score": r.score,
        "is_authoritative": r.is_authoritative,
        "on_retry_override": r.on_retry_override,
        "overridden": r.overridden,
        "evidence_event_ids": list(r.evidence_event_ids),
        "judge_binding": r.judge_binding,
        "computed_at": r.computed_at,
    }


def _attempt_dict(a: Any) -> dict[str, Any]:
    return {
        "attempt_id": a.attempt_id,
        "repeat_index": a.repeat_index,
        "attempt": a.attempt,
        "is_first_attempt": a.is_first_attempt,
        "status": a.status,
        "exit_code": a.exit_code,
        "event_count": a.event_count,
        "trace_truncated": a.trace_truncated,
        "error": a.error,
        "started_at": a.started_at,
        "finished_at": a.finished_at,
    }


def _revision_dict(r: Any) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "case_id": r.case_id,
        "metric_id": r.metric_id,
        "score_revision": r.score_revision,
        "machine_score_snapshot": r.machine_score_snapshot,
        "override_value": r.override_value,
        "dispute_path": r.dispute_path,
        "reason": r.reason,
        "author_id": r.author_id,
        "status": r.status,
        "created_at": r.created_at,
    }


def _run_metric_dict(m: Any) -> dict[str, Any]:
    return {
        "metric_id": m.metric_id,
        "value": m.value,
        "method": m.method,
        "aggregation_state": m.aggregation_state,
        "gate_status": m.gate_status,
        "no_ci": m.no_ci,
        "sample_n": m.sample_n,
        "error_n": m.error_n,
        "skipped_n": m.skipped_n,
        "computed_at": m.computed_at,
    }


def _specs_equal(a: str, b: str) -> bool:
    """Payload equality for idempotency replay (canonical JSON comparison)."""
    try:
        return json.loads(a) == json.loads(b)
    except Exception:
        return False


def _normalized_spec(spec: dict[str, Any] | str) -> str:
    """Canonical spec JSON for idempotency comparison.

    The body spec goes through the same pydantic validation as the engine's
    create path, so default fields and numeric normalizations (0 -> 0.0, nil
    optionals) match ``run.spec_json`` exactly.
    """
    return validate_spec(spec).model_dump_json()


_PIPELINE_STEPS = (
    ("connect", "Agent connected"),
    ("hitl", "HITL confirmed"),
    ("plan", "Eval plan & spec"),
    ("dataset", "Dataset built"),
    ("dashboard", "Dashboard authored"),
    ("run", "Agent executed"),
    ("score", "Scored"),
)


def _pipeline_for_eval(record: CustomEvalRecord, run_status: str | None) -> list[dict[str, Any]]:
    authored = bool(record.spec and record.dashboard)
    run_stage = "pending"
    score_stage = "pending"
    if run_status in ("complete",):
        run_stage = "complete"
        score_stage = "complete"
    elif run_status in ("failed", "cancelled", "incomplete"):
        run_stage = "failed"
        score_stage = "pending"
    elif run_status in ("draft", "queued", "provisioning", "running", "aggregating"):
        run_stage = "running"
        score_stage = "pending"
    statuses = {
        "connect": "complete" if record.source_path or record.entrypoint else "pending",
        "hitl": "complete" if record.entrypoint else "pending",
        "plan": "complete" if authored else "pending",
        "dataset": "complete" if record.dataset else "pending",
        "dashboard": "complete" if record.dashboard else "pending",
        "run": run_stage,
        "score": score_stage,
    }
    return [
        {"id": sid, "label": label, "status": statuses[sid]}
        for sid, label in _PIPELINE_STEPS
    ]


def _eval_payload(record: CustomEvalRecord, run_status: str | None = None) -> dict[str, Any]:
    return {
        "eval_id": record.eval_id,
        "name": record.name,
        "project_id": record.project_id,
        "spec": record.spec,
        "dashboard": record.dashboard,
        "dataset": record.dataset,
        "source_path": record.source_path,
        "entrypoint": record.entrypoint,
        "cwd": record.cwd,
        "run_id": record.run_id,
        "hitl": {
            "entrypoint": " ".join(record.entrypoint) if record.entrypoint else None,
            "source_path": record.source_path,
        },
        "pipeline": _pipeline_for_eval(record, run_status),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _safe_extract_zip(data: bytes, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest != target and dest not in target.parents:
                raise ValueError("zip archive contains an unsafe path")
        zf.extractall(dest)
    children = [p for p in dest.iterdir() if p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return dest


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    storage: Storage | None = None,
    engine: Engine | None = None,
    workspace_id: str = "default",
    auth_resolver: Any = None,
    artifact_root: Path | None = None,
    gateway: ModelGateway | None = None,
) -> FastAPI:
    """Build the application. Pass ``storage``/``engine`` to pin the store and
    engine (tests, ``eval-engine serve``); when omitted, the app lazily opens
    the configured default database on first use — constructing the app never
    creates a database file.

    ``workspace_id`` is the single-tenant workspace the app serves (§11A:
    multi-tenant schema, single-tenant deploy — RLS would inject the same id).
    """
    _default_storage: Storage | None = storage
    _default_engine: Engine | None = engine
    ws = workspace_id

    #: Background run workers: run_id -> thread, and run_id -> cancel event.
    #: Guarded by _registry_lock (worker threads and request threads race).
    workers: dict[str, threading.Thread] = {}
    cancel_events: dict[str, threading.Event] = {}
    registry_lock = threading.Lock()

    def store() -> Storage:
        nonlocal _default_storage
        if _default_storage is None:
            _default_storage = Storage(settings.db_path)
            _default_storage.create_schema()
        return _default_storage

    def eng() -> Engine:
        nonlocal _default_engine
        if _default_engine is None:
            _default_engine = Engine(store())
        return _default_engine

    def _run_worker(run_id: str, cancel_event: threading.Event) -> None:
        """The run executes here — never on the event loop. On success the
        engine has landed the run; on failure the engine itself lands it in
        ``incomplete`` (§11B.8) before propagating, so the worker only logs."""
        try:
            eng().run(run_id, ws, should_cancel=cancel_event.is_set)
        except RunAlreadyTerminalError:
            # Cancelled (or completed) before the worker picked it up — the
            # run is already terminal; nothing to do.
            pass
        except Exception:
            logger.exception("run %s failed in the background worker", run_id)
        finally:
            with registry_lock:
                workers.pop(run_id, None)
                cancel_events.pop(run_id, None)

    app = FastAPI(title="LLM Agent Evaluation Engine", version="0.1.0")

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next: Any) -> Any:
        # §17.10: every error response carries the correlation id.
        request.state.correlation_id = (
            request.headers.get("correlation_id")
            or request.headers.get("x-correlation-id")
            or uuid.uuid4().hex
        )
        try:
            actor = auth_resolver(request) if auth_resolver else Actor("local-owner", ws, "owner")
            if not isinstance(actor, Actor):
                raise WorkflowError("Authentication required", code="unauthorized", status=401)
            actor.require(workspace_id=ws, write=request.method not in {"GET", "HEAD", "OPTIONS"})
            request.state.actor = actor
        except WorkflowError as exc:
            response = _error(request, exc.status, exc.code, str(exc), exc.details)
            response.headers["x-correlation-id"] = request.state.correlation_id
            return response
        response = await call_next(request)
        response.headers["x-correlation-id"] = request.state.correlation_id
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css", ".json")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def _error(
        request: Request,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                    "correlation_id": getattr(request.state, "correlation_id", None),
                }
            },
        )

    # ---- §17.10 error envelope -------------------------------------------

    @app.exception_handler(WorkflowError)
    async def _workflow_error(request: Request, exc: WorkflowError) -> JSONResponse:
        return _error(request, exc.status, exc.code, str(exc), exc.details)

    from .workflow_api import workflow_router
    model_gateway = gateway or LiteLLMGateway(settings.model)
    router = workflow_router(store, artifact_root or Path(settings.artifact_root), settings.artifact_max_bytes, model_gateway)
    app.state.import_service = router.import_service
    app.state.workflow_worker = router.worker
    app.state.workflow_actor = Actor("local-owner", ws, "owner")
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]) or "body", "message": e["msg"]}
            for e in exc.errors()
        ]
        return _error(
            request, 422, "validation_error",
            "request validation failed", {"problems": problems},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "conflict"
        return _error(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(SpecValidationError)
    async def _spec_validation(request: Request, exc: SpecValidationError) -> JSONResponse:
        problems = [{"field": p.field, "message": p.message} for p in exc.problems]
        return _error(
            request, 422, "validation_error", str(exc), {"problems": problems},
        )

    @app.exception_handler(TierViolationError)
    async def _tier_violation(request: Request, exc: TierViolationError) -> JSONResponse:
        return _error(request, 422, "validation_error", str(exc))

    @app.exception_handler(ProjectNotEvaluableError)
    async def _not_evaluable(request: Request, exc: ProjectNotEvaluableError) -> JSONResponse:
        return _error(request, 409, "project_not_evaluable", str(exc))

    @app.exception_handler(RunAlreadyTerminalError)
    async def _already_terminal(request: Request, exc: RunAlreadyTerminalError) -> JSONResponse:
        return _error(request, 409, "state_conflict", str(exc))

    @app.exception_handler(IllegalTransition)
    async def _illegal_transition(request: Request, exc: IllegalTransition) -> JSONResponse:
        return _error(request, 409, "state_conflict", str(exc))

    @app.exception_handler(KeyError)
    async def _key_error(request: Request, exc: KeyError) -> JSONResponse:
        return _error(request, 404, "not_found", str(exc))

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        return _error(request, 422, "validation_error", str(exc))

    @app.exception_handler(sqlite3.Error)
    async def _db_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        logger.exception("database error")
        return _error(request, 500, "internal_error", "database error")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _error(request, 500, "internal_error", "internal server error")

    # ---- endpoints -------------------------------------------------------

    @app.get("/health")
    def health(request: Request) -> Any:
        try:
            store().get_run("__health_probe__", ws)
        except Exception:
            return _error(request, 503, "internal_error", "database unreachable")
        return {"status": "ok", "database": "ok", "service": "llm_agent_eval"}

    @app.post("/runs", status_code=201)
    def create_run(
        body: CreateRunRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> Any:
        """§17.4/§17.6 — create a run. ``spec`` is the evaluation spec (JSON
        object or text); ``tier`` defaults to the spec's run_tier hint. An
        ``idempotency_key`` (header or body) makes creation idempotent: a
        replay with an identical payload returns the original run (200,
        ``idempotent_replay: true``); the same key with a different payload
        is a 409 ``idempotency_conflict``."""
        key = idempotency_key or body.idempotency_key  # header precedence (§17.6)
        if key is not None:
            existing = store().get_run_by_idempotency_key(ws, key)
            if existing is not None:
                same = _specs_equal(_normalized_spec(body.spec), existing.spec_json) and (
                    body.tier is None or body.tier == existing.tier
                )
                if same:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "run": _run_summary(existing),
                            "idempotent_replay": True,
                            "state": "created",
                        },
                    )
                return _error(
                    request, 409, "idempotency_conflict",
                    f"Idempotency-Key {key!r} was already used for a different run payload "
                    "(§17.6) — use a fresh key for a new run",
                )
        try:
            run = eng().create_run(
                ws, body.spec, tier=body.tier, repeats=body.repeats,
                retry_max=body.retry_max, project_id=body.project_id,
                entrypoint=body.entrypoint, source_digest=body.source_digest,
                cwd=body.cwd, timeout_seconds=body.timeout_seconds,
                budget_usd_micros=body.budget_usd_micros,
                concurrency=body.concurrency, idempotency_key=key,
                created_by=body.created_by,
            )
        except ProjectNotEvaluableError as exc:
            project = None
            if body.project_id is not None:
                project = store().get_project(body.project_id, ws)
            return _error(
                request, 409, "project_not_evaluable", str(exc),
                {
                    "project_id": body.project_id,
                    "current_lifecycle_state": project.smoke_state if project else None,
                },
            )
        if key is not None and run.idempotency_key == key:
            # A concurrent double-submit raced the lookup above and bound the
            # key first; storage returns that run. Verify the payload actually
            # matches — otherwise this request must not claim the key.
            same = _specs_equal(_normalized_spec(body.spec), run.spec_json) and (
                body.tier is None or body.tier == run.tier
            )
            if not same:
                return _error(
                    request, 409, "idempotency_conflict",
                    f"Idempotency-Key {key!r} is bound to a different run payload (§17.6)",
                )
        return {
            "run": _run_summary(run),
            "idempotent_replay": False,
            "state": "created",
        }

    @app.get("/runs")
    def list_runs(
        request: Request,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> Any:
        """§17.9 — cursor pagination, stable order (created_at DESC, run_id
        DESC). ``next_cursor`` is opaque; omit ``cursor`` for the first page."""
        decoded = None
        if cursor is not None:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                decoded = (raw["created_at"], raw["run_id"])
            except Exception:
                return _error(request, 422, "validation_error", "malformed cursor")
        page, next_cursor = store().list_runs_cursor(ws, limit=limit, cursor=decoded)
        return {
            "runs": [_run_summary(r) for r in page],
            "limit": limit,
            "next_cursor": (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {"created_at": next_cursor[0], "run_id": next_cursor[1]},
                        sort_keys=True,
                    ).encode()
                ).decode()
                if next_cursor is not None
                else None
            ),
        }

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> Any:
        """§17.4 — run status with case scores, per-metric aggregation state,
        and the revision ledger. Per-case ``metrics`` carry the latest
        ``score_revision`` rows; ``attempts`` list every attempt (first vs
        retry) so both sides of a retry-replacement are surfaced."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        cases = store().list_cases(run_id, ws)
        case_scores = store().get_case_scores(run_id=run_id, workspace_id=ws)
        metric_results = store().get_run_metric_results(run_id, ws)
        revisions = store().get_score_revisions(run_id=run_id, workspace_id=ws)
        attempts = store().list_attempts(run_id, ws)

        latest: dict[tuple[str, str], Any] = {}
        for r in case_scores:
            key = (r.case_id, r.metric_id)
            if key not in latest or r.score_revision > latest[key].score_revision:
                latest[key] = r
        attempts_by_case: dict[str, list[Any]] = {}
        for a in attempts:
            attempts_by_case.setdefault(a.case_id, []).append(a)

        case_list: list[dict[str, Any]] = []
        for c in cases:
            rows = [
                latest[(c.case_id, m.metric_id)]
                for m in run.spec.metrics
                if (c.case_id, m.metric_id) in latest
            ]
            case_dict: dict[str, Any] = {
                "case_id": c.case_id,
                "case_key": c.case_key,
                "status": c.status,
                "classification": c.classification,
                "error_category": c.error_category,
                "repeat_count": c.repeat_count,
                "started_at": c.started_at,
                "completed_at": c.completed_at,
                "metrics": [_case_metric_dict(r) for r in rows],
                "attempts": [_attempt_dict(a) for a in attempts_by_case.get(c.case_id, [])],
            }
            case_list.append(case_dict)
        return {
            "run": _run_summary(run),
            "terminal": run.status in _TERMINAL_RUN_STATUSES,
            "cases": case_list,
            "metrics": [_run_metric_dict(m) for m in metric_results],
            "revisions": [_revision_dict(r) for r in revisions],
        }

    @app.post("/runs/{run_id}/start", status_code=202)
    def start_run(run_id: str, request: Request) -> Any:
        """§17.4 — dispatch the run to a background worker thread. The
        synchronous QUEUED transition is the observed state; the run then
        progresses provision -> running -> ... as the worker executes."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        status = RunStatus(run.status)
        if is_run_terminal(status):
            return _error(
                request, 409, "state_conflict",
                f"run {run_id!r} is already {status.value}; a terminal run does not re-run",
            )
        if status in (RunStatus.RUNNING, RunStatus.AGGREGATING, RunStatus.INCOMPLETE):
            return _error(
                request, 409, "state_conflict",
                f"run {run_id!r} is {status.value}; start is valid only for draft/queued runs",
            )
        with registry_lock:
            if run_id in workers:
                return _error(request, 409, "state_conflict", f"run {run_id!r} is already running")
            cancel_event = threading.Event()
            cancel_events[run_id] = cancel_event
            worker = threading.Thread(
                target=_run_worker,
                args=(run_id, cancel_event),
                daemon=True,
                name=f"run-{run_id}",
            )
            workers[run_id] = worker
        run = store().set_run_status(run_id, ws, RunStatus.QUEUED)
        worker.start()
        return {
            "run_id": run_id,
            "status": run.status,
            "state": "started",
            "message": "run dispatched to the background worker",
        }

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request) -> Any:
        """§17.7 — cancellation is a request, never a promise. With a live
        worker, the response is ``pending_cancel`` and the worker confirms by
        landing the run in ``cancelled`` (unrun cases CANCELLED as evidence,
        aggregates PARTIAL); the SSE stream emits ``run.cancelled`` then the
        terminal ``run.state``. Without a worker the transition happens
        directly (draft/queued -> cancelled)."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        status = RunStatus(run.status)
        if is_run_terminal(status):
            return {
                "run_id": run_id,
                "status": status.value,
                "state": "already_terminal",
                "message": "the run is already terminal; nothing to cancel",
            }
        with registry_lock:
            cancel_event = cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
            return {
                "run_id": run_id,
                "status": run.status,
                "state": "pending_cancel",
                "message": "cancel requested — the run transitions CANCELLED when the worker "
                "confirms at the next case boundary (§17.7)",
            }
        # No worker: the run has not started — cancel directly.
        try:
            store().set_run_status(run_id, ws, RunStatus.CANCELLED)
        except IllegalTransition:
            return _error(
                request, 409, "state_conflict",
                f"run {run_id!r} is {run.status!r} and cannot transition to cancelled",
            )
        return {
            "run_id": run_id,
            "status": "cancelled",
            "state": "cancelled",
            "message": "cancelled before dispatch",
        }

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, request: Request) -> Any:
        """§17.4 / §35B.3 — resume an interrupted run.

        MVP semantics (ledger R14): resume is an **in-place finish** of a
        non-terminal run. The engine re-scores already-finished cases from
        their stored traces (§13A.1.3 — no agent re-run for finished work,
        fresh container for whatever does re-run) and executes only the cases
        still queued or requeued by the reconciler. The §35B.3 continuation
        run (``parent_run_id``, merged aggregation into a new record) is the
        design's future mechanism; the in-place finish delivers the same user
        outcome for worker-crash / infra-failure interruption.

        Refusals: ``complete | failed`` runs never re-run (lifecycle §11B.8);
        ``cancelled`` runs DO resume — cancellation is a terminal outcome,
        the explicit resume verb requeues the run record (§35B.3, ledger
        R14); drafts have nothing to resume, ``/start`` is the right verb."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        status = RunStatus(run.status)
        if status in (RunStatus.COMPLETE, RunStatus.FAILED):
            return _error(
                request, 409, "state_conflict",
                f"run {run_id!r} is already {status.value}; a terminal run does not re-run",
            )
        if status is RunStatus.DRAFT:
            return _error(
                request, 409, "state_conflict",
                f"run {run_id!r} is {status.value}; resume is for interrupted runs — use /start",
            )
        with registry_lock:
            if run_id in workers:
                return _error(request, 409, "state_conflict", f"run {run_id!r} is already running")
            cancel_event = threading.Event()
            cancel_events[run_id] = cancel_event
            worker = threading.Thread(
                target=_run_worker,
                args=(run_id, cancel_event),
                daemon=True,
                name=f"run-{run_id}",
            )
            workers[run_id] = worker
        worker.start()
        return {
            "run_id": run_id,
            "status": run.status,
            "state": "resumed",
            "message": "run dispatched to the background worker for in-place resume "
            "(finished cases re-score from stored traces; interrupted cases re-run fresh)",
        }

    def _metric_event(run_id: str, m: Any) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "metric_id": m.metric_id,
            "value": m.value,
            "method": m.method,
            "aggregation_state": m.aggregation_state,
            "gate_status": m.gate_status,
            "sample_n": m.sample_n,
            "computed_at": m.computed_at,
        }

    @app.get("/runs/{run_id}/progress")
    async def run_progress(run_id: str, request: Request) -> Any:
        """§17.8 — SSE progress stream. Event names (verbatim from §17.8):
        ``run.state, case.completed, case.failed, metric.updated, run.failed,
        run.cancelled, run.resumed``. The stream ends with a terminal
        ``run.state`` carrying ``terminal: true`` and the final status. On
        connect it replays the current snapshot (survives a refresh, §35B.2);
        keepalive comments every 15s of silence."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")

        async def stream() -> Any:
            emitted_cases: set[str] = set()
            emitted_metrics: dict[str, str] = {}
            sent_snapshot = False
            last_event_at = time.monotonic()

            def sse(event: str, data: dict[str, Any]) -> str:
                return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

            while True:
                run = store().get_run(run_id, ws)
                if run is None:  # pragma: no cover - defensive
                    yield sse("run.state", {"run_id": run_id, "status": "not_found", "terminal": True})
                    return
                status = run.status
                if not sent_snapshot:
                    yield sse("run.state", {"run_id": run_id, "status": status})
                    for m in store().get_run_metric_results(run_id, ws):
                        yield sse("metric.updated", _metric_event(run_id, m))
                        emitted_metrics[m.metric_id] = m.computed_at
                    sent_snapshot = True
                    last_event_at = time.monotonic()
                for case in store().list_cases(run_id, ws):
                    if case.case_id in emitted_cases:
                        continue
                    if case.status == RunCaseStatus.COMPLETED.value:
                        yield sse("case.completed", {
                            "run_id": run_id,
                            "case_id": case.case_id,
                            "status": case.status,
                            "classification": case.classification,
                        })
                        emitted_cases.add(case.case_id)
                        last_event_at = time.monotonic()
                    elif case.status == RunCaseStatus.FAILED.value:
                        yield sse("case.failed", {
                            "run_id": run_id,
                            "case_id": case.case_id,
                            "status": case.status,
                            "classification": case.classification,
                            "error_category": case.error_category,
                        })
                        emitted_cases.add(case.case_id)
                        last_event_at = time.monotonic()
                for m in store().get_run_metric_results(run_id, ws):
                    if emitted_metrics.get(m.metric_id) != m.computed_at:
                        yield sse("metric.updated", _metric_event(run_id, m))
                        emitted_metrics[m.metric_id] = m.computed_at
                        last_event_at = time.monotonic()
                if status in _TERMINAL_RUN_STATUSES:
                    if status == RunStatus.CANCELLED.value:
                        yield sse("run.cancelled", {"run_id": run_id, "status": status})
                    elif status == RunStatus.FAILED.value:
                        yield sse("run.failed", {"run_id": run_id, "status": status})
                    yield sse(
                        "run.state",
                        {"run_id": run_id, "status": status, "terminal": True},
                    )
                    return
                if time.monotonic() - last_event_at >= 15:
                    yield ": keepalive\n\n"
                    last_event_at = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/traces/{case_id}")
    def get_case_traces(run_id: str, case_id: str, request: Request) -> Any:
        """Agent-under-test traces for one case (§17.4; the harness-session
        trace is a separate surface — the two are never conflated)."""
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        case = store().get_case(run_id, case_id, ws)
        if case is None:
            return _error(
                request, 404, "not_found", f"case {case_id!r} not found in run {run_id!r}"
            )
        events = store().get_trace_events(run_id=run_id, workspace_id=ws, case_id=case_id)
        return {
            "run_id": run_id,
            "case_id": case_id,
            "event_count": len(events),
            "events": [e.model_dump(mode="json") for e in events],
        }

    @app.post("/runs/{run_id}/metrics/{metric_id}/revisions", status_code=201)
    def create_revision(run_id: str, metric_id: str, body: RevisionRequest, request: Request) -> Any:
        """§13A.1.2 — an annotated override, stored ALONGSIDE the machine score
        (additive, idempotent by (run, case, metric, score_revision), diffable,
        annotated with dispute_path/reason/author). The machine row is never
        modified — only flagged ``overridden`` for ordering. The response
        carries both the applied revision and the machine score it supersedes.
        """
        run = store().get_run(run_id, ws)
        if run is None:
            return _error(request, 404, "not_found", f"run {run_id!r} not found")
        if not any(m.metric_id == metric_id for m in run.spec.metrics):
            return _error(
                request, 404, "not_found",
                f"metric {metric_id!r} is not in run {run_id!r}'s spec",
            )
        machine = store().get_case_scores(
            run_id=run_id, workspace_id=ws, case_id=body.case_id, metric_id=metric_id
        )
        if not machine:
            return _error(
                request, 404, "not_found",
                f"no machine score for ({run_id}, {body.case_id}, {metric_id}) — a "
                "revision references the metric result it revises (§13A.1)",
            )
        current = max(machine, key=lambda r: r.score_revision)
        revision = store().append_score_revision(
            run_id=run_id,
            case_id=body.case_id,
            metric_id=metric_id,
            workspace_id=ws,
            score_revision=body.score_revision,
            machine_score_snapshot={"status": current.status, "score": current.score},
            override_value=body.override_value,
            dispute_path=body.dispute_path,
            reason=body.reason,
            author_id=body.author_id,
        )
        # The append flags the machine row (overridden) in the same transaction;
        # re-read so the response shows the machine score as it now stands.
        flagged = store().get_case_scores(
            run_id=run_id, workspace_id=ws, case_id=body.case_id, metric_id=metric_id
        )
        machine = max(flagged, key=lambda r: r.score_revision)
        return {
            "state": "applied",
            "revision": _revision_dict(revision),
            "machine_score": {
                "score_revision": machine.score_revision,
                "status": machine.status,
                "score": machine.score,
                "is_authoritative": machine.is_authoritative,
                "on_retry_override": machine.on_retry_override,
                "overridden": machine.overridden,
            },
        }

    # ------------------------------------------------------------------
    # Dashboard surface (§37B) — the declarative definition is the only
    # thing the UI ever renders from; the API serves the registry and
    # server-side resolved data, never HTML/JS it invented.
    # ------------------------------------------------------------------

    @app.get("/api/dashboards/components")
    def list_dashboard_components() -> dict[str, Any]:
        comps = registry_components()
        return {
            "registry_version": get_registry_version(),
            "components": [
                {
                    "name": c.name,
                    "version": c.version,
                    "selection_type": c.selection_type,
                    "inputs": [
                        {
                            "name": i.name,
                            "type": i.type,
                            "required": i.required,
                            "values": list(i.values) if i.values else None,
                        }
                        for i in c.inputs
                    ],
                }
                for c in comps.values()
            ],
        }

    @app.get("/api/dashboards/default")
    def default_dashboard(run_id: str | None = None) -> dict[str, Any]:
        """Resolve the out-of-the-box dashboard server-side against
        pre-aggregated results (§37B.3). ``?run_id=`` pins the run filter;
        absent, the definition's ``latest`` default resolves it."""
        filters = {"run": run_id} if run_id else None
        resolved = resolve_definition(
            DEFAULT_DEFINITION, filters, None, store(), workspace_id=ws
        )
        return asdict(resolved)

    @app.post("/api/evaluations/validate")
    def validate_evaluation_spec(body: ValidateSpecRequest) -> dict[str, Any]:
        """Validate an evaluation spec against the schema (immediate, §32A.2)."""
        try:
            spec = validate_spec(body.spec)
            return {
                "valid": True,
                "validation_status": "validated",
                "verification_id": None,
                "verification_status": "not_verified",
                "spec": spec.model_dump(),
                "errors": [],
            }
        except SpecValidationError as exc:
            return {
                "valid": False,
                "validation_status": "failed",
                "verification_id": None,
                "verification_status": "not_verified",
                "spec": None,
                "errors": [{"field": p.field, "message": p.message} for p in exc.problems],
            }

    @app.post("/api/harness/author")
    def author_evaluation_spec(body: AuthorSpecRequest, request: Request) -> dict[str, Any]:
        """Author an evaluation spec from natural language intent (§10B/§31A)."""
        from .gateway import DeepSeekGateway
        from .harness import author_spec

        if not settings.model.has_key:
            template = {
                "spec_version": "0.1.0",
                "name": "authored-suite",
                "dataset_version": "v1",
                "run_tier": "quick",
                "cases": [
                    {
                        "case_id": "case_1",
                        "name": "Refund eligibility check",
                        "description": body.intent[:100],
                        "input": {"order_id": "ORD-1001", "query": body.intent},
                        "expected": {
                            "eligibility": {"tag": "user_stated", "value": True},
                        },
                    },
                ],
                "metrics": [
                    {
                        "metric_id": "eligibility_before_refund",
                        "name": "eligibility checked before refund",
                        "type": "trace_rule",
                        "target": {"type": "trace", "on_missing": "fail"},
                        "evaluator": {
                            "type": "trace_rule",
                            "rule": {
                                "op": "for_all",
                                "match": {"event": "tool_call", "tool": "refund_order"},
                                "assert": {"op": "exists_before", "match": {"event": "tool_call", "tool": "check_eligibility"}},
                            },
                        },
                        "scoring": {"type": "binary", "range": [0, 1]},
                        "aggregation": {"method": "pass_rate", "on_error": "fail"},
                    },
                ],
            }
            return {
                "state": "validated",
                "spec": template,
                "repair_attempts": 0,
                "expected_count": 1,
                "inferred_expected": 0,
                "inferred_share": 0.0,
                "notice": "Offline authoring template generated (set DEEPSEEK_API_KEY for dynamic LLM drafting).",
            }

        gateway = DeepSeekGateway(settings.model)
        res = author_spec(body.intent, gateway, repair_attempts=body.repair_attempts)
        if not res.validated or res.spec is None:
            return {
                "state": res.state,
                "reason": res.reason,
                "repair_attempts": res.repair_attempts,
            }
        return {
            "state": "validated",
            "spec": res.spec.model_dump(),
            "repair_attempts": res.repair_attempts,
            "expected_count": res.expected_count,
            "inferred_expected": res.inferred_expected,
            "inferred_share": res.inferred_share,
        }

    @app.post("/api/runs/sample", status_code=202)
    def start_sample_run(request: Request) -> dict[str, Any]:
        """Trigger an instant demonstration run using the sample agent fixture."""
        import sys
        sample_dir = Path(__file__).resolve().parents[2] / "fixtures" / "sample-agent"
        entrypoint = [sys.executable, str(sample_dir)]
        sample_spec = {
            "spec_version": "0.1.0",
            "name": "sample-support-triage",
            "dataset_version": "v1",
            "run_tier": "quick",
            "cases": [
                {
                    "case_id": f"refund-{i+1}",
                    "name": f"Refund request #{i+1}",
                    "input": {
                        "order_id": f"ORD-{1000 + i}",
                        "customer_message": f"Please refund order ORD-{1000 + i}",
                    },
                    "expected": {
                        "refund_amount": {"tag": "user_stated", "value": 49.99},
                    },
                }
                for i in range(3)
            ],
            "metrics": [
                {
                    "metric_id": "search_follows_llm",
                    "name": "search articles follows model decision",
                    "type": "trace_rule",
                    "target": {"type": "trace", "on_missing": "fail"},
                    "evaluator": {
                        "type": "trace_rule",
                        "rule": {
                            "op": "for_all",
                            "match": {"event": "tool_call", "tool": "search_articles"},
                            "assert": {"op": "exists_before", "match": {"event": "llm_response"}},
                        },
                    },
                    "scoring": {"type": "binary", "range": [0, 1]},
                    "aggregation": {"method": "pass_rate", "on_error": "fail"},
                },
                {
                    "metric_id": "eligibility_before_refund",
                    "name": "eligibility checked before refund",
                    "type": "trace_rule",
                    "target": {"type": "trace", "on_missing": "fail"},
                    "evaluator": {
                        "type": "trace_rule",
                        "rule": {
                            "op": "for_all",
                            "match": {"event": "tool_call", "tool": "process_refund"},
                            "assert": {"op": "exists_before", "match": {"event": "tool_call", "tool": "check_eligibility"}},
                        },
                    },
                    "scoring": {"type": "binary", "range": [0, 1]},
                    "aggregation": {"method": "pass_rate", "on_error": "fail"},
                },
            ],
        }
        run = eng().create_run(
            ws, sample_spec, tier="quick", repeats=1, retry_max=0,
            entrypoint=entrypoint, cwd=str(sample_dir),
        )
        start_run(run.run_id, request)
        return {
            "run_id": run.run_id,
            "status": "queued",
            "message": "Sample evaluation run initiated.",
        }

    @app.post("/api/projects/analyze")
    def analyze_project(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Statically inspect the explicitly selected local source (§32A)."""
        from .orchestrator import (
            RepoReaderAgent,
            RepoSummaryAgent,
            entrypoint_command_for_candidate,
            local_source_id,
        )

        source = body.get("source")
        if not isinstance(source, str) or not source.strip():
            return _error(
                request,
                422,
                "source_required",
                "an explicit source path is required",
            )
        source = source.strip()
        root = Path(__file__).resolve().parents[2]
        target_dir = root / source if not Path(source).is_absolute() else Path(source)
        if not target_dir.is_dir():
            return _error(
                request,
                404,
                "source_not_found",
                f"source {source!r} was not found",
            )
        target_dir = target_dir.resolve()
        is_explicit_example = target_dir == (root / "fixtures" / "sample-agent").resolve()

        scan = RepoReaderAgent().scan_directory(target_dir)
        analysis = RepoSummaryAgent().analyze_capabilities(
            target_dir,
            scan["files"],
            include_example_data=is_explicit_example,
        )

        entrypoint_name = scan["entrypoint_candidates"][0] if scan["entrypoint_candidates"] else None
        entrypoint_command = entrypoint_command_for_candidate(target_dir, entrypoint_name)

        project_name = target_dir.name
        project = store().create_project(
            workspace_id=ws,
            name=project_name,
            entrypoint=entrypoint_command,
        )

        return {
            "project_id": project.project_id,
            "source_id": local_source_id(target_dir),
            "source_path": str(target_dir),
            "name": project_name,
            "status": "ANALYZED",
            "summary": analysis["summary"],
            "entrypoint": f"python {entrypoint_name}" if entrypoint_name else None,
            "entrypoint_command": entrypoint_command,
            "tools_detected": analysis["tools_detected"],
            "models_detected": analysis["models_detected"],
            "files": scan["files"],
            "findings": analysis["findings"],
            "diagnostics": analysis["diagnostics"],
            "sample_data": analysis["sample_data"],
            "suggested_evals": [],
            "verification_id": None,
            "verification_status": "not_verified",
            "verification_message": "Not verified.",
            "message": "Static analysis completed. Runtime has not been verified.",
        }

    @app.post("/api/projects/upload")
    async def upload_agent_archive(file: UploadFile = File(...)) -> dict[str, Any]:
        """Accept a ZIP of the agent under test and unpack it for Builder chat."""
        raw = await file.read()
        stem = Path(file.filename or "agent").stem or "agent"
        dest = Path(tempfile.gettempdir()) / "llm_agent_eval" / "uploads" / uuid.uuid4().hex / stem
        try:
            source = _safe_extract_zip(raw, dest)
        except (ValueError, zipfile.BadZipFile) as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
        return {
            "name": source.name,
            "source": str(source),
            "message": "Archive unpacked. Connect it in chat to author an eval.",
        }

    @app.get("/api/evals")
    def list_custom_evals() -> dict[str, Any]:
        records = store().list_custom_evals(ws)
        items = []
        for rec in records:
            run_status = None
            if rec.run_id:
                run = store().get_run(rec.run_id, ws)
                run_status = run.status if run else None
            items.append(_eval_payload(rec, run_status))
        return {"evals": items}

    @app.get("/api/evals/{eval_id}")
    def get_custom_eval(eval_id: str, request: Request) -> Any:
        rec = store().get_custom_eval(eval_id, ws)
        if rec is None:
            return _error(request, 404, "not_found", f"eval {eval_id!r} not found")
        run_status = None
        if rec.run_id:
            run = store().get_run(rec.run_id, ws)
            run_status = run.status if run else None
        return _eval_payload(rec, run_status)

    @app.post("/api/evals/{eval_id}/run", status_code=202)
    def run_custom_eval(eval_id: str, request: Request) -> Any:
        """Execute the connected agent against this eval's authored spec and dataset."""
        rec = store().get_custom_eval(eval_id, ws)
        if rec is None:
            return _error(request, 404, "not_found", f"eval {eval_id!r} not found")
        if not rec.entrypoint:
            return _error(request, 409, "validation_error", "eval has no agent entrypoint")
        try:
            run = eng().create_run(
                ws, rec.spec, tier=rec.spec.get("run_tier") or "quick",
                repeats=1, retry_max=0,
                entrypoint=rec.entrypoint, cwd=rec.cwd,
            )
        except (ValueError, SpecValidationError, EngineError) as exc:
            return _error(request, 422, "validation_error", str(exc))
        store().set_custom_eval_run(eval_id, ws, run.run_id)
        start_run(run.run_id, request)
        return {
            "eval_id": eval_id,
            "run_id": run.run_id,
            "status": "queued",
            "message": "Custom evaluation run initiated against the connected agent.",
        }

    @app.post("/api/harness/chat")
    def harness_chat_turn(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Conversational turn endpoint with multi-agent orchestration and cross-communication (§31A)."""
        from dataclasses import asdict
        from .orchestrator import OrchestratorAgent, SourceNotFoundError

        msg = (body.get("message") or "").strip()
        project_id = body.get("project_id")
        eval_id = body.get("eval_id")

        orch = OrchestratorAgent()
        try:
            result = orch.process_turn(msg, project_id=project_id)
        except SourceNotFoundError as exc:
            return _error(request, 404, "source_not_found", str(exc))

        eval_record = None
        if result.project_data:
            try:
                p = store().create_project(
                    workspace_id=ws,
                    name=result.project_data["name"],
                    entrypoint=result.project_data.get("entrypoint_command") or [],
                )
                result.project_data["project_id"] = p.project_id
                project_id = p.project_id
            except Exception:
                pass

        if result.spec_data and result.dashboard:
            source = result.source_path
            cwd = source
            entrypoint = [sys.executable, source] if source else []
            eval_record = store().create_custom_eval(
                workspace_id=ws,
                name=result.spec_data.get("name") or "custom-eval",
                spec=result.spec_data,
                dashboard=result.dashboard,
                dataset=result.spec_data.get("cases") or [],
                project_id=project_id,
                source_path=source,
                entrypoint=entrypoint,
                cwd=cwd,
                eval_id=eval_id,
            )

        payload = {
            "role": "assistant",
            "action": result.action,
            "reply": result.orchestrator_summary,
            "agent_steps": [asdict(s) for s in result.agent_steps],
            "data": result.project_data,
            "spec_data": result.spec_data,
            "dashboard": result.dashboard or (eval_record.dashboard if eval_record else None),
            "hitl": None,
            "eval_id": eval_record.eval_id if eval_record else eval_id,
            "pipeline": _eval_payload(eval_record).get("pipeline") if eval_record else [],
            "suggestions": result.suggestions,
        }
        if eval_record:
            payload["hitl"] = {
                "entrypoint": " ".join(eval_record.entrypoint),
                "tools_detected": (result.project_data or {}).get("tools_detected") or [],
                "models_detected": (result.project_data or {}).get("models_detected") or [],
                "files": (result.project_data or {}).get("files") or [],
                "verification_id": (result.project_data or {}).get("verification_id"),
                "verification_status": (result.project_data or {}).get("verification_status"),
                "verification_message": (result.project_data or {}).get("verification_message"),
            }
        return payload

    # The dashboard UI is a static single-page app; mount it only when
    # present — the backend is fully usable headless (the API is the contract).
    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


_APP: FastAPI | None = None
_APP_LOCK = threading.Lock()


def get_app() -> FastAPI:
    """The conventional application instance, created lazily on first use so
    importing this module never opens a database file. ``eval-engine serve``
    and tests construct their own app via :func:`create_app`."""
    global _APP
    if _APP is None:
        with _APP_LOCK:
            if _APP is None:
                _APP = create_app()
    return _APP


def __getattr__(name: str) -> Any:
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
