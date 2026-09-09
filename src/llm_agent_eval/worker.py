"""One worker drain for import and authoring jobs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .auth import Actor
from .authoring import AuthoringService
from .contracts import WorkflowError
from .gateway import ModelGateway
from .ingestion import ImportFailure, ImportService
from .jobs import JobQueue
from .previews import PreviewService
from .sessions import SessionStore
from .targets import ConnectionService
from .versions import VersionStore


class WorkflowWorker:
    def __init__(self, storage, artifact_root, gateway: ModelGateway):
        self.storage = storage
        self.artifact_root = Path(artifact_root)
        self.gateway = gateway
        self.fingerprint_key = hashlib.sha256(("workflow:" + str(self.artifact_root.resolve())).encode()).digest()
        self.importer = ImportService(storage, artifact_root)
        self.authoring = AuthoringService(storage, gateway)
        self.previews = PreviewService(storage, artifact_root)
        os.environ.setdefault("LLM_AGENT_EVAL_SERVICE_IDENTITY", "eval-engine")
        self.connections = ConnectionService(storage, self.artifact_root / "install-secret.key")

    def queue(self, actor: Actor) -> JobQueue:
        return JobQueue(self.storage, actor, fingerprint_key=self.fingerprint_key)

    def drain(self, actor: Actor, job_id: str):
        queue = self.queue(actor)
        job = queue.claim_specific(job_id, "workflow-worker")
        if job is None:
            return queue.get(job_id)
        try:
            kind = job.command.get("kind")
            if kind == "source_import":
                result = self.importer._process(actor, job.command)
            elif kind == "session_turn":
                result = self.authoring.execute(actor, job.command)
            elif kind == "dashboard_preview":
                result = self.previews.execute(actor, job.command)
            elif kind == "target_verify":
                result = self.connections.verify(actor, job.command["target_id"], job.command)
            else:
                raise WorkflowError(f"Unknown job kind {kind!r}")
        except ImportFailure as exc:
            return queue.fail(job.job_id, job.fence, {"code": exc.code})
        except WorkflowError as exc:
            return queue.fail(job.job_id, job.fence, {"code": exc.code})
        except Exception as exc:
            return queue.fail(job.job_id, job.fence, {"code": "job_failed", "detail": type(exc).__name__})
        return queue.complete(job.job_id, job.fence, result)

    def submit_turn(self, actor: Actor, session_id: str, body: dict, idempotency_key: str):
        actor.require(write=True)
        session = SessionStore(self.storage).get(actor, session_id)
        if body.get("expected_revision") != session["revision"]:
            from .contracts import RevisionConflict
            raise RevisionConflict(session["revision"])
        job = self.queue(actor).enqueue({
            "kind": "session_turn",
            "session_id": session_id,
            "evaluation_id": session["evaluation_id"],
            "message": body.get("message"),
            "card_action": body.get("card_action"),
            "expected_revision": body["expected_revision"],
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
            "kind": "dashboard_preview",
            "dashboard_version_id": dashboard_version_id,
            "evaluation_version_id": refs.get("evaluation_version_id"),
            "dataset_version_id": refs.get("dataset_version_id"),
            "generator_version": refs.get("generator_version") or "1",
        }
        if not command["evaluation_version_id"] or not command["dataset_version_id"]:
            raise WorkflowError("evaluation_version_id and dataset_version_id are required")
        return self.queue(actor).enqueue(command, idempotency_key)

    def submit_verify(self, actor: Actor, target_id: str, body: dict, idempotency_key: str):
        actor.require(write=True)
        command = {"kind": "target_verify", "target_id": target_id, **body}
        return self.queue(actor).enqueue(command, idempotency_key)
