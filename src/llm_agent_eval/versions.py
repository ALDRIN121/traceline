"""Immutable authoring versions and a compare-and-swap active pointer."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict

from .auth import Actor
from .contracts import NotFound, RevisionConflict, VERSION_KINDS, VersionRecord, WorkflowError
from .storage import Storage, _now


def canonical_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("Content must be finite JSON") from exc


def _record(row) -> VersionRecord:
    return VersionRecord(
        **{field: row[field] for field in (
            "version_id", "workspace_id", "kind", "parent_id", "previous_version_id",
            "content_digest", "revision", "actor_id", "created_at")},
        content=json.loads(row["content_json"]), authoring_provenance=json.loads(row["provenance_json"]),
    )


class VersionStore:
    def __init__(self, storage: Storage):
        self.storage = storage

    def _parent(self, conn, parent_id, actor):
        for table, key in (("projects", "project_id"), ("custom_evals", "eval_id"),
                           ("datasets", "dataset_id"), ("targets", "target_id")):
            found = conn.execute(f"SELECT {key} FROM {table} WHERE {key}=? AND workspace_id=?",
                                 (parent_id, actor.workspace_id)).fetchone()
            if found is not None:
                return
        raise NotFound()

    def create(self, kind, parent_id, content, expected_revision, actor: Actor) -> VersionRecord:
        actor.require(write=True)
        if kind not in VERSION_KINDS or not isinstance(content, dict):
            raise WorkflowError("A supported version kind and JSON object are required")
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkflowError("expected_revision must be a nonnegative integer")
        if {"workspace_id", "actor_id", "created_at", "revision"}.intersection(content):
            raise WorkflowError("Identity and revision come from trusted context")
        encoded = canonical_json(content)
        # Round-trip detaches caller-owned nested lists/maps from the persisted snapshot.
        content = json.loads(encoded)
        provenance = content.get("authoring_provenance", {"type": "human", "actor_id": actor.actor_id})
        if not isinstance(provenance, dict):
            raise WorkflowError("authoring_provenance must be an object")
        artifact_ids = content.get("artifact_ids", [])
        if not isinstance(artifact_ids, list) or not all(isinstance(i, str) for i in artifact_ids):
            raise WorkflowError("artifact_ids must be a list of IDs")
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            self._parent(conn, parent_id, actor)
            conn.execute(
                "INSERT INTO version_heads (workspace_id,kind,parent_id,revision) VALUES (?,?,?,0) "
                "ON CONFLICT (workspace_id,kind,parent_id) DO NOTHING", (actor.workspace_id, kind, parent_id),
            )
            suffix = " FOR UPDATE" if self.storage._is_postgres else ""
            head = conn.execute(
                "SELECT * FROM version_heads WHERE workspace_id=? AND kind=? AND parent_id=?" + suffix,
                (actor.workspace_id, kind, parent_id),
            ).fetchone()
            if head["revision"] != expected_revision:
                raise RevisionConflict(head["revision"])
            for artifact_id in artifact_ids:
                if conn.execute("SELECT artifact_id FROM artifacts WHERE workspace_id=? AND artifact_id=? AND state='ready'",
                                (actor.workspace_id, artifact_id)).fetchone() is None:
                    raise NotFound()
            record = VersionRecord(uuid.uuid4().hex, actor.workspace_id, kind, parent_id,
                                   head["active_version_id"], hashlib.sha256(encoded.encode()).hexdigest(),
                                   expected_revision + 1, actor.actor_id, _now(), content, provenance)
            conn.execute(
                "INSERT INTO object_versions (workspace_id,version_id,kind,parent_id,previous_version_id,"
                "content_digest,revision,actor_id,created_at,content_json,provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (record.workspace_id, record.version_id, kind, parent_id, record.previous_version_id,
                 record.content_digest, record.revision, actor.actor_id, record.created_at, encoded, canonical_json(provenance)),
            )
            for artifact_id in set(artifact_ids):
                conn.execute("INSERT INTO version_artifacts (workspace_id,version_id,artifact_id) VALUES (?,?,?)",
                             (actor.workspace_id, record.version_id, artifact_id))
            updated = conn.execute(
                "UPDATE version_heads SET active_version_id=?, revision=? WHERE workspace_id=? AND kind=? AND parent_id=? AND revision=?",
                (record.version_id, record.revision, actor.workspace_id, kind, parent_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(expected_revision)
        return record

    def get(self, version_id, actor: Actor) -> VersionRecord:
        actor.require()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            row = conn.execute("SELECT * FROM object_versions WHERE workspace_id=? AND version_id=?",
                               (actor.workspace_id, version_id)).fetchone()
            if row is None:
                raise NotFound()
            return _record(row)

    def list(self, kind, parent_id, actor: Actor) -> dict:
        actor.require()
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            self._parent(conn, parent_id, actor)
            rows = conn.execute("SELECT * FROM object_versions WHERE workspace_id=? AND kind=? AND parent_id=? ORDER BY revision",
                                (actor.workspace_id, kind, parent_id)).fetchall()
            head = conn.execute("SELECT * FROM version_heads WHERE workspace_id=? AND kind=? AND parent_id=?",
                                (actor.workspace_id, kind, parent_id)).fetchone()
            return {"versions": [asdict(_record(row)) for row in rows],
                    "active_version_id": head["active_version_id"] if head else None,
                    "active_revision": head["revision"] if head else 0}

    def migrate_legacy(self, actor: Actor) -> list[dict]:
        """Snapshot valid definitions and quarantine invalid ones, retaining runs.

        This explicit compatibility migration is repeatable. It never edits the
        original custom_evals row or a run's frozen spec. Full dashboard shape
        conversion belongs to T09; malformed legacy definitions stay quarantined.
        """
        from .dashboard import canonicalize_dashboard, validate_definition
        from .spec import validate_spec

        actor.require(write=True)
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            rows = conn.execute("SELECT * FROM custom_evals WHERE workspace_id=?", (actor.workspace_id,)).fetchall()
            for row in rows:
                for kind, column, validate in (("evaluation", "spec_json", validate_spec),
                                               ("dashboard", "dashboard_json", validate_definition)):
                    digest = hashlib.sha256(row[column].encode()).hexdigest()
                    existing = conn.execute("SELECT state FROM legacy_definition_migrations WHERE workspace_id=? AND eval_id=? AND kind=? AND source_digest=?",
                                            (actor.workspace_id, row["eval_id"], kind, digest)).fetchone()
                    if existing:
                        continue
                    version_id, reason, state = None, None, "quarantined"
                    try:
                        content = json.loads(row[column])
                        validate(content)
                    except (ValueError, TypeError) as exc:
                        # Only validation summaries, never raw legacy content or paths.
                        reason = f"Invalid legacy {kind}: {type(exc).__name__}"
                    else:
                        head = self.list(kind, row["eval_id"], actor)
                        if head["active_revision"]:
                            reason = "Legacy content changed after a durable version was created; explicit review required"
                        else:
                            if kind == "dashboard":
                                content = canonicalize_dashboard(content)
                            version_id = self.create(kind, row["eval_id"], content, 0, actor).version_id
                            state = "migrated"
                    conn.execute("INSERT INTO legacy_definition_migrations (workspace_id,eval_id,kind,source_digest,version_id,state,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (actor.workspace_id, row["eval_id"], kind, digest, version_id, state, reason, _now()))
            result = conn.execute("SELECT * FROM legacy_definition_migrations WHERE workspace_id=? ORDER BY eval_id,kind,created_at",
                                  (actor.workspace_id,)).fetchall()
            return [dict(row) for row in result]
