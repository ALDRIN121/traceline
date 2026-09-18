"""Local operational checks and recoverable workspace backups.

The service is deliberately conservative: SQLite backups are self-contained
and restore never overwrites an existing destination. PostgreSQL production
backups remain the operator's ``pg_dump``/artifact-store procedure and are
reported as an explicit unsupported local operation rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import PurePosixPath
from typing import Any

from .artifacts import ArtifactStore
from .contracts import WorkflowError


class OperationsService:
    def __init__(self, storage, artifact_root: Path):
        self.storage = storage
        self.artifact_root = Path(artifact_root).resolve()

    def readiness(self, workspace_id: str) -> dict[str, Any]:
        checks: dict[str, str] = {}
        try:
            self.storage.get_run("__readiness_probe__", workspace_id)
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "failed"
        checks["artifact_root"] = (
            "ok"
            if self.artifact_root.is_dir() and os.access(self.artifact_root, os.W_OK)
            else "failed"
        )
        ready = all(value == "ok" for value in checks.values())
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    def workspace_usage(self, workspace_id: str) -> dict[str, int]:
        with self.storage.workspace_transaction(workspace_id) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM artifacts WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return {"artifact_count": int(row[0]), "artifact_bytes": int(row[1])}

    def enforce_artifact_quota(self, workspace_id: str, additional_bytes: int, limit_bytes: int) -> None:
        if type(additional_bytes) is not int or additional_bytes < 0:
            raise WorkflowError("additional_bytes must be nonnegative")
        if type(limit_bytes) is not int or limit_bytes <= 0:
            raise WorkflowError("limit_bytes must be positive")
        usage = self.workspace_usage(workspace_id)
        if usage["artifact_bytes"] + additional_bytes > limit_bytes:
            raise WorkflowError(
                "workspace artifact quota exceeded", code="quota_exceeded", status=429,
                details={"used_bytes": usage["artifact_bytes"], "limit_bytes": limit_bytes},
            )

    def recover_workspace(self, actor) -> dict[str, Any]:
        return ArtifactStore(self.storage, self.artifact_root, actor).recover(actor.workspace_id)

    def backup_workspace(self, actor, destination: Path) -> dict[str, Any]:
        actor.require(write=True)
        if self.storage._is_postgres:
            raise WorkflowError(
                "PostgreSQL backup must use the operator's coordinated database and artifact-store procedure",
                code="external_backup_required", status=409,
            )
        destination = Path(destination).resolve()
        if destination.exists():
            raise WorkflowError("backup destination already exists", code="backup_exists", status=409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="eval-backup-", dir=destination.parent) as temporary:
            root = Path(temporary)
            db_path = root / "database.sqlite"
            source = self.storage._conn
            with sqlite3.connect(db_path) as target:
                source.backup(target)
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifacts = ArtifactStore(self.storage, self.artifact_root, actor).list(actor.workspace_id)
            checksums: dict[str, str] = {}
            for record in artifacts:
                data = ArtifactStore(self.storage, self.artifact_root, actor).get(actor.workspace_id, record.artifact_id)
                path = artifact_dir / record.artifact_id
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                checksums[f"artifacts/{record.artifact_id}"] = hashlib.sha256(data).hexdigest()
            checksums["database.sqlite"] = hashlib.sha256(db_path.read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps({
                "schema": 1, "workspace_id": actor.workspace_id,
                "files": checksums,
            }, sort_keys=True), encoding="utf-8")
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
        return {"state": "ready", "path": str(destination), "workspace_id": actor.workspace_id}

    @staticmethod
    def restore_sqlite(backup: Path, destination_db: Path, destination_artifact_root: Path) -> dict[str, Any]:
        backup, destination_db, destination_artifact_root = map(lambda p: Path(p).resolve(), (backup, destination_db, destination_artifact_root))
        if destination_db.exists():
            raise WorkflowError("restore destination already exists", code="restore_exists", status=409)
        with tempfile.TemporaryDirectory(prefix="eval-restore-") as temporary:
            root = Path(temporary).resolve()
            try:
                with zipfile.ZipFile(backup) as archive:
                    for member in archive.infolist():
                        relative = PurePosixPath(member.filename)
                        if (relative.is_absolute() or ".." in relative.parts
                                or "\\" in member.filename or not relative.parts):
                            raise WorkflowError("backup contains an unsafe path", code="backup_invalid", status=422)
                        destination = (root / relative.as_posix()).resolve()
                        if not destination.is_relative_to(root):
                            raise WorkflowError("backup contains an unsafe path", code="backup_invalid", status=422)
                        if member.is_dir():
                            destination.mkdir(parents=True, exist_ok=True)
                        else:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, destination.open("xb") as target:
                                shutil.copyfileobj(source, target)
            except (OSError, zipfile.BadZipFile) as exc:
                raise WorkflowError("backup archive is invalid", code="backup_invalid", status=422) from exc
            manifest_path = root / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                files = manifest["files"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise WorkflowError("backup manifest is invalid", code="backup_invalid", status=422) from exc
            for relative, expected in files.items():
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise WorkflowError("backup checksum verification failed", code="backup_corrupt", status=422)
            destination_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "database.sqlite", destination_db)
            if destination_artifact_root.exists():
                raise WorkflowError("artifact restore destination already exists", code="restore_exists", status=409)
            shutil.copytree(root / "artifacts", destination_artifact_root, dirs_exist_ok=False)
        return {"state": "restored", "database": str(destination_db), "artifact_root": str(destination_artifact_root)}
