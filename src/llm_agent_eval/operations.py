"""Local operational checks and recoverable workspace backups.

The service is deliberately conservative: SQLite backups are self-contained
and restore never overwrites an existing destination. PostgreSQL production
backups use an explicit maintenance connection for the database dump and the
application connection only for workspace-scoped artifact enumeration. The
maintenance URL is never stored in the archive.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .artifacts import ArtifactStore
from .contracts import WorkflowError


class OperationsService:
    def __init__(self, storage, artifact_root: Path, *, maintenance_database_url: str | None = None):
        self.storage = storage
        self.artifact_root = Path(artifact_root).resolve()
        self.maintenance_database_url = maintenance_database_url or os.environ.get(
            "LLM_AGENT_EVAL_MAINTENANCE_DATABASE_URL"
        )

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

    def enforce_retention(
        self,
        actor,
        *,
        now: datetime | None = None,
        export_days: int = 7,
        preview_days: int = 30,
        abandoned_import_hours: int = 24,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply the conservative local retention policy from workflow §12.

        Database references and immutable version attachments are checked before
        filesystem deletion.  ``dry_run`` returns the exact candidate counts
        without changing rows or files; a scheduler can therefore report the
        impact before the operator enables pruning.
        """
        actor.require(write=True)
        for name, value, low in (
            ("export_days", export_days, 1),
            ("preview_days", preview_days, 1),
            ("abandoned_import_hours", abandoned_import_hours, 1),
        ):
            if type(value) is not int or value < low:
                raise WorkflowError(f"{name} must be an integer >= {low}")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise WorkflowError("retention now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        export_cutoff = (current - timedelta(days=export_days)).isoformat()
        preview_cutoff = (current - timedelta(days=preview_days)).isoformat()
        upload_cutoff = (current - timedelta(hours=abandoned_import_hours)).timestamp()
        preview_ids = self.storage.preview_artifact_ids_before(actor.workspace_id, preview_cutoff)
        protected_job_ids = self.storage.all_job_artifact_ids(actor.workspace_id) - preview_ids
        preview_ids -= protected_job_ids
        export_rows = self._expired_export_count(actor.workspace_id, export_cutoff)
        upload_paths = self._old_uploads(actor.workspace_id, upload_cutoff)
        candidates = {
            "expired_exports": export_rows,
            "preview_artifacts": len(preview_ids),
            "abandoned_uploads": len(upload_paths),
            "staging_files": self._old_staging_files(actor.workspace_id, upload_cutoff),
        }
        if dry_run:
            return {"state": "dry_run", **candidates}
        deleted_exports = self.storage.delete_export_snapshots_before(
            actor.workspace_id, export_cutoff,
        )
        artifact_store = ArtifactStore(self.storage, self.artifact_root, actor)
        deleted_preview = sum(
            1 for artifact_id in sorted(preview_ids)
            if artifact_store.delete_unreferenced(actor.workspace_id, artifact_id)
        )
        deleted_uploads = 0
        for path in upload_paths:
            try:
                path.unlink()
                deleted_uploads += 1
            except FileNotFoundError:
                pass
        deleted_staging = self._delete_old_staging(actor.workspace_id, upload_cutoff)
        return {
            "state": "completed",
            "expired_exports": deleted_exports,
            "preview_artifacts": deleted_preview,
            "abandoned_uploads": deleted_uploads,
            "staging_files": deleted_staging,
        }

    def _expired_export_count(self, workspace_id: str, cutoff: str) -> int:
        with self.storage.workspace_transaction(workspace_id) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM export_snapshots WHERE workspace_id=? AND created_at < ?",
                (workspace_id, cutoff),
            ).fetchone()[0])

    def _workspace_dir(self, workspace_id: str, name: str) -> Path:
        if not isinstance(workspace_id, str) or not workspace_id.replace("_", "").replace("-", "").isalnum():
            raise WorkflowError("invalid workspace identity")
        return (self.artifact_root / name / workspace_id).resolve()

    def _old_uploads(self, workspace_id: str, cutoff: float) -> list[Path]:
        root = self._workspace_dir(workspace_id, "quarantine")
        if not root.is_dir():
            return []
        return [
            path for path in root.iterdir()
            if path.is_file() and path.name != ".keep" and path.stat().st_mtime < cutoff
        ]

    def _old_staging_files(self, workspace_id: str, cutoff: float) -> int:
        root = (self.artifact_root / "ws" / workspace_id / "artifacts").resolve()
        if not root.is_dir():
            return 0
        return sum(1 for path in root.iterdir() if path.is_file() and path.suffix == ".stage" and path.stat().st_mtime < cutoff)

    def _delete_old_staging(self, workspace_id: str, cutoff: float) -> int:
        root = (self.artifact_root / "ws" / workspace_id / "artifacts").resolve()
        if not root.is_dir():
            return 0
        deleted = 0
        for path in root.iterdir():
            if path.is_file() and path.suffix == ".stage" and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        return deleted

    def backup_postgres(
        self, actor, destination: Path, *, maintenance_database_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a coordinated PostgreSQL dump plus workspace artifact archive.

        The database dump and artifact files share one checksum manifest.  The
        install encryption/identity keys are deliberately excluded; operators
        must back those up through their separate key-management procedure.
        """
        actor.require(write=True)
        if not self.storage._is_postgres:
            raise WorkflowError("PostgreSQL storage is required", code="storage_kind_invalid", status=409)
        maintenance_url = maintenance_database_url or self.maintenance_database_url
        if not maintenance_url:
            raise WorkflowError(
                "an explicit PostgreSQL maintenance URL is required for backup",
                code="maintenance_database_url_required", status=503,
            )
        dump_tool = shutil.which("pg_dump")
        if dump_tool is None:
            raise WorkflowError("pg_dump is required for PostgreSQL backup", code="backup_tool_unavailable", status=503)
        destination = Path(destination).resolve()
        if destination.exists():
            raise WorkflowError("backup destination already exists", code="backup_exists", status=409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        database_url, environment = self._safe_database_command(maintenance_url)
        with tempfile.TemporaryDirectory(prefix="eval-pg-backup-", dir=destination.parent) as temporary:
            root = Path(temporary)
            dump = root / "database.dump"
            completed = subprocess.run(
                [dump_tool, "--format=custom", "--no-owner", "--file", str(dump), database_url],
                check=False, capture_output=True, env=environment,
            )
            if completed.returncode != 0 or not dump.is_file():
                raise WorkflowError("PostgreSQL dump failed", code="backup_failed", status=503)
            self._write_artifact_bundle(root, actor)
            checksums = {
                relative.as_posix(): hashlib.sha256((root / relative).read_bytes()).hexdigest()
                for relative in [PurePosixPath("database.dump")]
            }
            checksums.update({
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / "artifacts").rglob("*") if path.is_file()
            })
            (root / "manifest.json").write_text(json.dumps({
                "schema": 1, "storage": "postgresql", "workspace_id": actor.workspace_id,
                "install_keys": "excluded_from_archive_restore_separately", "files": checksums,
            }, sort_keys=True), encoding="utf-8")
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
        return {
            "state": "ready", "path": str(destination), "workspace_id": actor.workspace_id,
            "storage": "postgresql", "install_keys": "excluded_from_archive_restore_separately",
        }

    @staticmethod
    def _safe_database_command(database_url: str) -> tuple[str, dict[str, str]]:
        if not isinstance(database_url, str) or not database_url.startswith(("postgresql://", "postgres://")):
            raise WorkflowError("PostgreSQL database URL is required", code="database_url_invalid", status=503)
        parsed = urlsplit(database_url)
        if not parsed.hostname:
            raise WorkflowError("PostgreSQL database URL is invalid", code="database_url_invalid", status=503)
        password = parsed.password
        username = parsed.username
        netloc = ""
        if username:
            netloc += username + "@"
        if parsed.hostname:
            if ":" in parsed.hostname and not parsed.hostname.startswith("["):
                netloc += f"[{parsed.hostname}]"
            else:
                netloc += parsed.hostname
        if parsed.port:
            netloc += f":{parsed.port}"
        safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        environment = dict(os.environ)
        if password is not None:
            environment["PGPASSWORD"] = unquote(password)
        else:
            environment.pop("PGPASSWORD", None)
        return safe_url, environment

    def _write_artifact_bundle(self, root: Path, actor) -> None:
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        store = ArtifactStore(self.storage, self.artifact_root, actor)
        for record in store.list(actor.workspace_id):
            path = artifact_dir / record.artifact_id
            path.write_bytes(store.get(actor.workspace_id, record.artifact_id))

    @staticmethod
    def _restore_artifacts(root: Path, destination_artifact_root: Path, workspace_id: str) -> None:
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
            raise WorkflowError("backup manifest workspace is invalid", code="backup_invalid", status=422)
        artifact_destination = (
            destination_artifact_root / "ws" / workspace_id / "artifacts"
        ).resolve()
        destination_root = destination_artifact_root.resolve()
        if not artifact_destination.is_relative_to(destination_root):
            raise WorkflowError("backup manifest workspace is invalid", code="backup_invalid", status=422)
        artifact_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / "artifacts", artifact_destination, dirs_exist_ok=False)

    @staticmethod
    def restore_postgres(
        backup: Path, destination_database_url: str, destination_artifact_root: Path,
    ) -> dict[str, Any]:
        """Restore and verify a PostgreSQL/artifact bundle into fresh targets."""
        backup, destination_artifact_root = Path(backup).resolve(), Path(destination_artifact_root).resolve()
        if destination_artifact_root.exists():
            raise WorkflowError("artifact restore destination already exists", code="restore_exists", status=409)
        restore_tool = shutil.which("pg_restore")
        if restore_tool is None:
            raise WorkflowError("pg_restore is required for PostgreSQL restore", code="restore_tool_unavailable", status=503)
        database_url, environment = OperationsService._safe_database_command(destination_database_url)
        with tempfile.TemporaryDirectory(prefix="eval-pg-restore-") as temporary:
            root = Path(temporary).resolve()
            try:
                with zipfile.ZipFile(backup) as archive:
                    for member in archive.infolist():
                        relative = PurePosixPath(member.filename)
                        if (relative.is_absolute() or ".." in relative.parts
                                or "\\" in member.filename or not relative.parts):
                            raise WorkflowError("backup contains an unsafe path", code="backup_invalid", status=422)
                        target = (root / relative.as_posix()).resolve()
                        if not target.is_relative_to(root):
                            raise WorkflowError("backup contains an unsafe path", code="backup_invalid", status=422)
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, target.open("xb") as destination:
                                shutil.copyfileobj(source, destination)
            except (OSError, zipfile.BadZipFile) as exc:
                raise WorkflowError("backup archive is invalid", code="backup_invalid", status=422) from exc
            try:
                manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("storage") != "postgresql" or manifest.get("install_keys") != "excluded_from_archive_restore_separately":
                    raise ValueError
                files = manifest["files"]
                workspace_id = manifest["workspace_id"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise WorkflowError("backup manifest is invalid", code="backup_invalid", status=422) from exc
            for relative, expected in files.items():
                path = (root / relative).resolve()
                if (not path.is_relative_to(root) or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                    raise WorkflowError("backup checksum verification failed", code="backup_corrupt", status=422)
            dump = root / "database.dump"
            if not dump.is_file():
                raise WorkflowError("backup database dump is missing", code="backup_invalid", status=422)
            (root / "artifacts").mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [restore_tool, "--exit-on-error", "--no-owner", "--dbname", database_url, str(dump)],
                check=False, capture_output=True, env=environment,
            )
            if completed.returncode != 0:
                raise WorkflowError("PostgreSQL restore failed", code="restore_failed", status=503)
            OperationsService._restore_artifacts(
                root, destination_artifact_root, workspace_id,
            )
        return {
            "state": "restored", "database": database_url,
            "artifact_root": str(destination_artifact_root),
            "storage": "postgresql", "install_keys": "required_separately",
        }

    def backup_workspace(
        self, actor, destination: Path, *, maintenance_database_url: str | None = None,
    ) -> dict[str, Any]:
        actor.require(write=True)
        if self.storage._is_postgres:
            return self.backup_postgres(
                actor, destination, maintenance_database_url=maintenance_database_url,
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
                "schema": 1, "storage": "sqlite", "workspace_id": actor.workspace_id,
                "install_keys": "excluded_from_archive_restore_separately",
                "files": checksums,
            }, sort_keys=True), encoding="utf-8")
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
        return {
            "state": "ready", "path": str(destination), "workspace_id": actor.workspace_id,
            "storage": "sqlite", "install_keys": "excluded_from_archive_restore_separately",
        }

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
                if manifest.get("storage") != "sqlite" or manifest.get("install_keys") != "excluded_from_archive_restore_separately":
                    raise ValueError
                files = manifest["files"]
                workspace_id = manifest["workspace_id"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise WorkflowError("backup manifest is invalid", code="backup_invalid", status=422) from exc
            for relative, expected in files.items():
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise WorkflowError("backup checksum verification failed", code="backup_corrupt", status=422)
            (root / "artifacts").mkdir(parents=True, exist_ok=True)
            destination_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "database.sqlite", destination_db)
            if destination_artifact_root.exists():
                raise WorkflowError("artifact restore destination already exists", code="restore_exists", status=409)
            OperationsService._restore_artifacts(
                root, destination_artifact_root, workspace_id,
            )
        return {
            "state": "restored", "database": str(destination_db),
            "artifact_root": str(destination_artifact_root),
            "storage": "sqlite", "install_keys": "required_separately",
        }
