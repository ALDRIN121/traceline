"""Durable harness session pointers. Messages are redacted before write."""

from __future__ import annotations

import json
import uuid

from .auth import Actor
from .contracts import NotFound, RevisionConflict, WorkflowError
from .redaction import redact
from .storage import Storage, _now


class SessionStore:
    def __init__(self, storage: Storage):
        self.storage = storage

    def create(self, actor: Actor, project_id: str, evaluation_id: str, *,
               evaluation_version_id=None, knowledge_version_id=None) -> dict:
        actor.require(write=True)
        if self.storage.get_project(project_id, actor.workspace_id) is None:
            raise WorkflowError("Project not found", code="not_found", status=404)
        session_id, now = uuid.uuid4().hex, _now()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            conn.execute(
                "INSERT INTO harness_sessions (workspace_id,session_id,project_id,evaluation_id,"
                "evaluation_version_id,knowledge_version_id,verification_id,revision,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,NULL,0,?,?)",
                (actor.workspace_id, session_id, project_id, evaluation_id,
                 evaluation_version_id, knowledge_version_id, now, now),
            )
        return self.get(actor, session_id)

    def get(self, actor: Actor, session_id: str) -> dict:
        actor.require()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            row = conn.execute(
                "SELECT * FROM harness_sessions WHERE workspace_id=? AND session_id=?",
                (actor.workspace_id, session_id),
            ).fetchone()
            if row is None:
                raise NotFound()
            return dict(row)

    def append_message(self, actor: Actor, session_id: str, role: str, kind: str, content,
                       *, operation_id=None, job_id=None, observed_state=None) -> None:
        actor.require(write=True)
        safe = redact(content)
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            if conn.execute("SELECT session_id FROM harness_sessions WHERE workspace_id=? AND session_id=?",
                            (actor.workspace_id, session_id)).fetchone() is None:
                raise NotFound()
            conn.execute(
                "INSERT INTO harness_messages (workspace_id,message_id,session_id,role,kind,content_json,"
                "operation_id,job_id,observed_state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (actor.workspace_id, uuid.uuid4().hex, session_id, role, kind,
                 json.dumps(safe.content, sort_keys=True), operation_id, job_id, observed_state, _now()),
            )

    def advance(self, actor: Actor, session_id: str, expected_revision: int, **fields) -> dict:
        actor.require(write=True)
        now = _now()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            row = conn.execute(
                "SELECT * FROM harness_sessions WHERE workspace_id=? AND session_id=?",
                (actor.workspace_id, session_id),
            ).fetchone()
            if row is None:
                raise NotFound()
            if int(row["revision"]) != expected_revision:
                raise RevisionConflict(int(row["revision"]))
            evaluation_version_id = fields.get("evaluation_version_id", row["evaluation_version_id"])
            updated = conn.execute(
                "UPDATE harness_sessions SET evaluation_version_id=?, revision=?, updated_at=? "
                "WHERE workspace_id=? AND session_id=? AND revision=?",
                (evaluation_version_id, expected_revision + 1, now,
                 actor.workspace_id, session_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(expected_revision)
        return self.get(actor, session_id)
