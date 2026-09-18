"""Workflow submission and durable service dispatch; no API-owned execution loop."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .auth import Actor
from .authoring import AuthoringService
from .contracts import WorkflowError
from .execution import RunExecutionService
from .runtime.source import SourceRuntimeService
from .gateway import ModelGateway
from .ingestion import ImportService
from .jobs import JobQueue
from .previews import PreviewService
from .sessions import SessionStore
from .targets import ConnectionService
from .versions import VersionStore
from .worker_service import JobContext, PreparedResult, WorkerService


class WorkflowWorker:
    def __init__(self, storage, artifact_root, gateway: ModelGateway, *,
                 fingerprint_key: bytes | None = None,
                 fingerprint_key_source: Callable[[], bytes] | None = None,
                 handlers: dict[str, Callable] | None = None, lease_seconds: int = 30,
                 execution_target_factory: Callable | None = None):
        material = fingerprint_key
        if material is None and fingerprint_key_source is not None:
            material = fingerprint_key_source()
        if not isinstance(material, bytes) or len(material) < 32:
            raise WorkflowError("An install-owned 32+ byte fingerprint key is required")
        self.fingerprint_key = material
        self.storage = storage
        self.artifact_root = Path(artifact_root)
        self.gateway = gateway
        self.handlers = dict(handlers or {})
        self.lease_seconds = lease_seconds
        self.authoring = AuthoringService(storage, gateway)
        # Submission-facing services stay public for the API layer; execution
        # builds guarded, lease-validated instances inside the dispatch path.
        self.importer = ImportService(storage, artifact_root)
        self.previews = PreviewService(storage, artifact_root)
        self.connections = ConnectionService(storage, self.artifact_root / "install-secret.key")
        self.execution_target_factory = execution_target_factory

    def queue(self, actor: Actor) -> JobQueue:
        return JobQueue(self.storage, actor, fingerprint_key=self.fingerprint_key,
                        lease_seconds=self.lease_seconds)

    def service(self, actor: Actor, *, worker_id=None) -> WorkerService:
        return WorkerService(self.queue(actor), self._dispatch, worker_id=worker_id)

    def run_once(self, actor: Actor, *, worker_id=None, stop_event=None):
        return self.service(actor, worker_id=worker_id).run_once(stop_event=stop_event)

    def run_forever(self, actor: Actor, *, stop_event, poll_seconds=0.5, worker_id=None):
        return self.service(actor, worker_id=worker_id).run_forever(
            stop_event=stop_event, poll_seconds=poll_seconds,
        )

    def _dispatch(self, actor: Actor, command: dict, context: JobContext):
        context.checkpoint()
        kind = command.get("kind")
        if kind in self.handlers:
            return self.handlers[kind](actor, command, context)
        storage = context.storage
        if kind == "source_import":
            return ImportService(storage, self.artifact_root)._process(actor, command)
        if kind == "source_prepare":
            version = SourceRuntimeService(storage, self.artifact_root, actor).prepare(
                command["source_version_id"], command["runtime_profile"],
            )
            return {"state": "executable", "source_version_id": version.version_id,
                    "source_digest": version.content_digest}
        if kind == "session_turn":
            authoring = AuthoringService(storage, self.authoring.gateway)
            prepared = authoring.prepare(actor, command)
            # The final revision, session pointer and job result share one
            # fenced transaction, after the provider call has completed.
            return PreparedResult(lambda: authoring.publish(actor, command, prepared))
        if kind == "dashboard_preview":
            return PreviewService(storage, self.artifact_root).execute(actor, command)
        if kind == "target_verify":
            return ConnectionService(storage, self.artifact_root / "install-secret.key").verify(
                actor, command["target_id"], command,
            )
        if kind == "evaluation_run":
            return RunExecutionService(
                storage, self.artifact_root, target_factory=self.execution_target_factory,
            ).execute(actor, command, context)
        raise WorkflowError("Unknown job kind", code="unknown_job_kind")

    def drain(self, actor: Actor, job_id: str):
        """Compatibility for explicit development tests; not a release HTTP route."""
        result = self.service(actor).run_once(job_id=job_id)
        return result if result is not None else self.queue(actor).get(job_id)

    def submit_turn(self, actor: Actor, session_id: str, body: dict, idempotency_key: str):
        actor.require(write=True)
        with self.storage.workspace_transaction(actor.workspace_id):
            session = SessionStore(self.storage).get(actor, session_id)
            if body.get("expected_revision") != session["revision"]:
                from .contracts import RevisionConflict
                raise RevisionConflict(session["revision"])
            job = self.queue(actor).enqueue({
                "kind": "session_turn", "session_id": session_id,
                "evaluation_id": session["evaluation_id"], "message": body.get("message"),
                "card_action": body.get("card_action"), "expected_revision": body["expected_revision"],
                "operation_id": idempotency_key,
            }, idempotency_key)
            SessionStore(self.storage).append_message(
                actor, session_id, "user", "turn",
                {"message": body.get("message"), "card_action": body.get("card_action")},
                operation_id=idempotency_key, job_id=job.job_id, observed_state="queued",
            )
        return job

    def submit_preview(self, actor: Actor, dashboard_version_id: str, refs: dict, idempotency_key: str):
        actor.require(write=True)
        VersionStore(self.storage).get(dashboard_version_id, actor)
        command = {
            "kind": "dashboard_preview", "dashboard_version_id": dashboard_version_id,
            "evaluation_version_id": refs.get("evaluation_version_id"),
            "dataset_version_id": refs.get("dataset_version_id"),
            "generator_version": refs.get("generator_version") or "1",
        }
        if not command["evaluation_version_id"] or not command["dataset_version_id"]:
            raise WorkflowError("evaluation_version_id and dataset_version_id are required")
        return self.queue(actor).enqueue(command, idempotency_key)

    def submit_verify(self, actor: Actor, target_id: str, body: dict, idempotency_key: str):
        actor.require(write=True)
        command = {**body, "kind": "target_verify", "target_id": target_id}
        return self.queue(actor).enqueue(command, idempotency_key)
