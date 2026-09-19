"""Checksummed, workspace-scoped local artifacts with atomic publication.

Metadata becomes ready only after a staged file is fsynced, verified and
renamed. Recovery removes uncommitted files after a process crash while
retaining committed evidence. Local volume encryption is deployment-owned.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

from .auth import Actor
from .contracts import ArtifactRecord, NotFound, WorkflowError
from .storage import Storage, _now


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ArtifactStore:
    def __init__(self, storage: Storage, root: Path, actor: Actor):
        self.storage, self.root, self.actor = storage, Path(root).resolve(), actor

    def _directory(self, workspace_id):
        self.actor.require(workspace_id=workspace_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
            raise WorkflowError("Invalid workspace identity")
        path = self.root / "ws" / workspace_id / "artifacts"
        if not path.resolve().is_relative_to(self.root):
            raise NotFound()
        return path

    def _lock(self, conn, workspace_id):
        if self.storage._is_postgres:
            # A transaction lock also serializes cleanup with puts from other processes.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                         (str(self.root) + ":" + workspace_id,)).fetchall()

    def put(self, workspace_id: str, data: bytes, media_type: str) -> ArtifactRecord:
        self.actor.require(write=True, workspace_id=workspace_id)
        if not isinstance(data, bytes) or not data:
            raise WorkflowError("Artifact must contain bytes")
        if not isinstance(media_type, str) or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            raise WorkflowError("Invalid media type")
        directory = self._directory(workspace_id)
        checksum = hashlib.sha256(data).hexdigest()
        record = ArtifactRecord(uuid.uuid4().hex, workspace_id, checksum, len(data), media_type, _now())
        stage, final = None, directory / record.artifact_id
        try:
            with self.storage.workspace_transaction(workspace_id) as conn:
                self._lock(conn, workspace_id)
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".stage", delete=False) as file:
                    stage = Path(file.name)
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                if hashlib.sha256(stage.read_bytes()).hexdigest() != checksum:
                    raise WorkflowError("Artifact checksum mismatch", code="artifact_corrupt", status=500)
                os.replace(stage, final)
                _fsync_directory(directory)
                conn.execute("INSERT INTO artifacts (workspace_id,artifact_id,checksum,size_bytes,media_type,created_at,state) VALUES (?,?,?,?,?,?,'ready')",
                             (workspace_id, record.artifact_id, checksum, len(data), media_type, record.created_at))
        except BaseException:
            if stage is not None:
                stage.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        return record

    def record(self, workspace_id, artifact_id) -> ArtifactRecord:
        self.actor.require(workspace_id=workspace_id)
        with self.storage.workspace_transaction(workspace_id) as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE workspace_id=? AND artifact_id=? AND state='ready'",
                               (workspace_id, artifact_id)).fetchone()
            if row is None:
                raise NotFound()
            return ArtifactRecord(**dict(row))

    def get(self, workspace_id: str, artifact_id: str) -> bytes:
        record = self.record(workspace_id, artifact_id)
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise NotFound()
        path = self._directory(workspace_id) / artifact_id
        if path.is_symlink():
            raise NotFound()
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise WorkflowError("Artifact is unavailable", code="artifact_unavailable", status=409) from exc
        if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.checksum:
            raise WorkflowError("Artifact checksum mismatch", code="artifact_corrupt", status=409)
        return data

    def list(self, workspace_id):
        self.actor.require(workspace_id=workspace_id)
        with self.storage.workspace_transaction(workspace_id) as conn:
            rows = conn.execute("SELECT * FROM artifacts WHERE workspace_id=? ORDER BY created_at,artifact_id", (workspace_id,)).fetchall()
            return [ArtifactRecord(**dict(row)) for row in rows]

    def recover(self, workspace_id) -> dict:
        """Reconcile this workspace's files; never enumerate other tenants."""
        self.actor.require(write=True, workspace_id=workspace_id)
        removed, unavailable = 0, []
        with self.storage.workspace_transaction(workspace_id) as conn:
            self._lock(conn, workspace_id)
            directory = self._directory(workspace_id)
            records = self.list(workspace_id)
            committed = {record.artifact_id for record in records}
            if directory.exists():
                for path in directory.iterdir():
                    if path.is_file() and path.name not in committed and (path.suffix == ".stage" or re.fullmatch(r"[0-9a-f]{32}", path.name)):
                        path.unlink()
                        removed += 1
            for record in records:
                try:
                    self.get(workspace_id, record.artifact_id)
                except WorkflowError:
                    unavailable.append(record.artifact_id)
        return {"removed_uncommitted": removed, "unavailable_artifact_ids": unavailable}

    def delete_unreferenced(self, workspace_id: str, artifact_id: str) -> bool:
        """Delete one ready artifact only when no immutable version references it."""
        self.actor.require(write=True, workspace_id=workspace_id)
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise NotFound()
        with self.storage.workspace_transaction(workspace_id) as conn:
            referenced = conn.execute(
                "SELECT 1 FROM version_artifacts WHERE workspace_id=? AND artifact_id=? LIMIT 1",
                (workspace_id, artifact_id),
            ).fetchone()
            if referenced is not None:
                return False
            deleted = conn.execute(
                "DELETE FROM artifacts WHERE workspace_id=? AND artifact_id=? AND state='ready'",
                (workspace_id, artifact_id),
            )
            if deleted.rowcount != 1:
                return False
        path = self._directory(workspace_id) / artifact_id
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.exists():
            path.unlink()
        return True
