"""Durable workspace-scoped jobs with transactional outbox and lease fencing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .redaction import RedactedPayload, redact
from .storage import Storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    workspace_id: str
    command: dict[str, Any]
    command_digest: str
    idempotency_key: str
    status: str
    attempts: int
    max_attempts: int
    fence: int
    lease_worker_id: str | None
    lease_expires_at: str | None
    cancellation_requested: bool
    result: Any | None
    error: Any | None
    command_redaction: dict[str, Any]
    result_redaction: dict[str, Any] | None
    created_at: str
    updated_at: str


LeasedJob = JobRecord


class StaleLease(WorkflowError):
    def __init__(self):
        super().__init__("Job lease is stale or no longer active", code="stale_lease", status=409)


class JobQueue:
    def __init__(self, storage: Storage, actor: Actor, *, fingerprint_key: bytes,
                 lease_seconds: int = 30, clock: Callable[[], str] = _now):
        if not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.storage, self.actor = storage, actor
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise ValueError("an install-owned 32-byte idempotency key is required")
        self.fingerprint_key = fingerprint_key
        self.lease_seconds, self.clock = lease_seconds, clock

    @property
    def workspace_id(self) -> str:
        self.actor.require(workspace_id=self.actor.workspace_id)
        return self.actor.workspace_id

    def _row(self, row: Any) -> JobRecord:
        if row is None:
            raise NotFound()
        data = dict(row)
        return JobRecord(
            job_id=data["job_id"], workspace_id=data["workspace_id"],
            command=json.loads(data["command_json"]), command_digest=data["command_digest"],
            idempotency_key=data["idempotency_key"], status=data["status"],
            attempts=int(data["attempts"]), max_attempts=int(data["max_attempts"]), fence=int(data["fence"]),
            lease_worker_id=data["lease_worker_id"], lease_expires_at=data["lease_expires_at"],
            cancellation_requested=bool(data["cancellation_requested"]),
            result=json.loads(data["result_json"]) if data["result_json"] else None,
            error=json.loads(data["error_json"]) if data["error_json"] else None,
            command_redaction=json.loads(data["command_redaction_json"]),
            result_redaction=json.loads(data["result_redaction_json"]) if data["result_redaction_json"] else None,
            created_at=data["created_at"], updated_at=data["updated_at"],
        )

    @staticmethod
    def _redaction_data(value: RedactedPayload) -> dict[str, Any]:
        return {"version": value.version, "detector_flags": list(value.detector_flags), "truncated": value.truncated}

    def _outbox(self, conn: Any, job_id: str, event_type: str, now: str) -> None:
        conn.execute(
            "INSERT INTO job_outbox (workspace_id,event_id,job_id,event_type,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (self.workspace_id, uuid.uuid4().hex, job_id, event_type, _canonical({"job_id": job_id, "event_type": event_type}), now),
        )

    def enqueue(self, command: dict[str, Any], idempotency_key: str, *, max_attempts: int = 3) -> JobRecord:
        self.actor.require(write=True, workspace_id=self.workspace_id)
        if not isinstance(command, dict) or not command:
            raise WorkflowError("Job command must be a non-empty object")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 255:
            raise WorkflowError("A bounded idempotency key is required")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
            raise WorkflowError("max_attempts must be between 1 and 20")
        safe = redact(command)
        command_json = _canonical(safe.content)
        digest = hmac.digest(self.fingerprint_key, _canonical(command).encode("utf-8"), "sha256").hex()
        now = self.clock()
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND idempotency_key=?", (self.workspace_id, idempotency_key)
            ).fetchone()
            if existing is not None:
                record = self._row(existing)
                if record.command_digest != digest:
                    raise WorkflowError("Idempotency key is already bound to a different command", code="idempotency_conflict", status=409)
                return record
            job_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO jobs (workspace_id,job_id,command_json,command_digest,idempotency_key,status,attempts,max_attempts,fence,lease_worker_id,lease_expires_at,cancellation_requested,result_json,error_json,command_redaction_json,result_redaction_json,created_at,updated_at) VALUES (?,?,?,?,?,'queued',0,?,0,NULL,NULL,0,NULL,NULL,?,NULL,?,?)",
                (self.workspace_id, job_id, command_json, digest, idempotency_key, max_attempts, _canonical(self._redaction_data(safe)), now, now),
            )
            self._outbox(conn, job_id, "queued", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def get(self, job_id: str) -> JobRecord:
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def _expire_and_cancel(self, conn: Any, now: str) -> None:
        rows = conn.execute(
            "SELECT job_id,attempts,max_attempts,cancellation_requested FROM jobs WHERE workspace_id=? AND status='leased' AND lease_expires_at<=?",
            (self.workspace_id, now),
        ).fetchall()
        for row in rows:
            next_status = "cancelled" if bool(row["cancellation_requested"]) else (
                "failed" if int(row["attempts"]) >= int(row["max_attempts"]) else "queued"
            )
            error = _canonical({"code": "retry_exhausted"}) if next_status == "failed" else None
            conn.execute(
                "UPDATE jobs SET status=?,lease_worker_id=NULL,lease_expires_at=NULL,error_json=?,updated_at=? WHERE workspace_id=? AND job_id=? AND status='leased'",
                (next_status, error, now, self.workspace_id, row["job_id"]),
            )
            self._outbox(conn, row["job_id"], next_status, now)

    def claim(self, worker_id: str) -> LeasedJob | None:
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id is required")
        self.actor.require(write=True, workspace_id=self.workspace_id)
        now = self.clock()
        lease_until = (datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            self._expire_and_cancel(conn, now)
            row = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND status='queued' AND cancellation_requested=0 ORDER BY created_at,job_id LIMIT 1",
                (self.workspace_id,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE jobs SET status='leased',attempts=attempts+1,fence=fence+1,lease_worker_id=?,lease_expires_at=?,updated_at=? WHERE workspace_id=? AND job_id=? AND status='queued' AND cancellation_requested=0",
                (worker_id, lease_until, now, self.workspace_id, row["job_id"]),
            )
            if not updated.rowcount:
                return None
            self._outbox(conn, row["job_id"], "leased", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, row["job_id"])).fetchone())

    def claim_specific(self, job_id: str, worker_id: str) -> LeasedJob | None:
        """Atomically lease this job only; never consume another queued job."""
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id is required")
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id is required")
        self.actor.require(write=True, workspace_id=self.workspace_id)
        now = self.clock()
        lease_until = (datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            self._expire_and_cancel(conn, now)
            updated = conn.execute(
                "UPDATE jobs SET status='leased',attempts=attempts+1,fence=fence+1,lease_worker_id=?,lease_expires_at=?,updated_at=? WHERE workspace_id=? AND job_id=? AND status='queued' AND cancellation_requested=0",
                (worker_id, lease_until, now, self.workspace_id, job_id),
            )
            if not updated.rowcount:
                return None
            self._outbox(conn, job_id, "leased", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def heartbeat(self, job_id: str, fence: int) -> LeasedJob:
        self.actor.require(write=True, workspace_id=self.workspace_id)
        now = self.clock()
        lease_until = (datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            updated = conn.execute(
                "UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE workspace_id=? AND job_id=? AND status='leased' AND fence=? AND lease_expires_at>? AND cancellation_requested=0",
                (lease_until, now, self.workspace_id, job_id, fence, now),
            )
            if not updated.rowcount:
                raise StaleLease()
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def complete(self, job_id: str, fence: int, result: Any) -> JobRecord:
        self.actor.require(write=True, workspace_id=self.workspace_id)
        now = self.clock()
        safe = redact(result)
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            updated = conn.execute(
                "UPDATE jobs SET status='completed',result_json=?,result_redaction_json=?,lease_worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE workspace_id=? AND job_id=? AND status='leased' AND fence=? AND lease_expires_at>? AND cancellation_requested=0",
                (_canonical(safe.content), _canonical(self._redaction_data(safe)), now, self.workspace_id, job_id, fence, now),
            )
            if not updated.rowcount:
                raise StaleLease()
            self._outbox(conn, job_id, "completed", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def fail(self, job_id: str, fence: int, error: dict[str, Any]) -> JobRecord:
        """Land an observed worker failure without manufacturing a result."""
        self.actor.require(write=True, workspace_id=self.workspace_id)
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise WorkflowError("A typed job error is required")
        now = self.clock()
        safe = redact(error)
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            updated = conn.execute(
                "UPDATE jobs SET status='failed',error_json=?,result_redaction_json=?,lease_worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE workspace_id=? AND job_id=? AND status='leased' AND fence=? AND lease_expires_at>?",
                (_canonical(safe.content), _canonical(self._redaction_data(safe)), now, self.workspace_id, job_id, fence, now),
            )
            if not updated.rowcount:
                raise StaleLease()
            self._outbox(conn, job_id, "failed", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def request_cancel(self, job_id: str) -> JobRecord:
        self.actor.require(write=True, workspace_id=self.workspace_id)
        now = self.clock()
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            record = self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())
            if record.status in {"completed", "failed", "cancelled"}:
                return record
            status = "cancelled" if record.status == "queued" else record.status
            conn.execute(
                "UPDATE jobs SET cancellation_requested=1,status=?,lease_worker_id=CASE WHEN ?='cancelled' THEN NULL ELSE lease_worker_id END,lease_expires_at=CASE WHEN ?='cancelled' THEN NULL ELSE lease_expires_at END,updated_at=? WHERE workspace_id=? AND job_id=?",
                (status, status, status, now, self.workspace_id, job_id),
            )
            self._outbox(conn, job_id, "cancellation_requested" if status != "cancelled" else "cancelled", now)
            return self._row(conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND job_id=?", (self.workspace_id, job_id)).fetchone())

    def pending_outbox(self) -> list[dict[str, Any]]:
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM job_outbox WHERE workspace_id=? AND published_at IS NULL ORDER BY created_at,event_id", (self.workspace_id,)).fetchall()]

    def acknowledge_outbox(self, event_id: str) -> None:
        self.actor.require(write=True, workspace_id=self.workspace_id)
        with self.storage.workspace_transaction(self.workspace_id) as conn:
            conn.execute("UPDATE job_outbox SET published_at=? WHERE workspace_id=? AND event_id=? AND published_at IS NULL", (self.clock(), self.workspace_id, event_id))
