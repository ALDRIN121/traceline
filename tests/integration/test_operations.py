from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import subprocess
from pathlib import Path

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.operations import OperationsService
import llm_agent_eval.operations as operations_module
from llm_agent_eval.storage import Storage
from llm_agent_eval.contracts import WorkflowError


def test_sqlite_backup_restore_verifies_manifest_and_artifacts(tmp_path):
    db = tmp_path / "source.db"
    artifacts = tmp_path / "artifacts"
    storage = Storage(db)
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        record = ArtifactStore(storage, artifacts, actor).put("ws", b"evidence", "text/plain")
        backup = tmp_path / "backup.zip"
        result = OperationsService(storage, artifacts).backup_workspace(actor, backup)
        assert result["state"] == "ready"
    finally:
        storage.close()

    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-artifacts"
    restored = OperationsService.restore_sqlite(backup, restored_db, restored_artifacts)
    assert restored["state"] == "restored"
    restored_storage = Storage(restored_db)
    restored_storage.create_schema()
    try:
        assert restored_storage.get_run("__missing__", "ws") is None
        assert ArtifactStore(
            restored_storage, restored_artifacts, actor,
        ).get("ws", record.artifact_id) == b"evidence"
    finally:
        restored_storage.close()


def test_retention_expires_exports_previews_and_abandoned_uploads_without_deleting_referenced_artifacts(tmp_path):
    db = tmp_path / "retention.db"
    artifacts = tmp_path / "artifacts"
    storage = Storage(db)
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    old = (now - timedelta(days=31)).isoformat()
    try:
        artifact_store = ArtifactStore(storage, artifacts, actor)
        previews = [
            artifact_store.put("ws", f"preview-{index}".encode(), "text/html")
            for index in range(21)
        ]
        referenced = ArtifactStore(storage, artifacts, actor).put("ws", b"source", "application/zip")
        with storage.workspace_transaction("ws") as conn:
            for index, preview in enumerate(previews):
                conn.execute(
                    "INSERT INTO jobs (workspace_id,job_id,command_json,command_digest,idempotency_key,status,attempts,max_attempts,fence,lease_worker_id,lease_expires_at,cancellation_requested,result_json,error_json,command_redaction_json,result_redaction_json,created_at,updated_at) VALUES (?,?,?,?,?,'completed',1,1,1,NULL,NULL,0,?,?,?,NULL,?,?)",
                    ("ws", f"{index + 1:032x}", json.dumps({"kind": "dashboard_preview"}), "d", f"preview-old-{index}",
                     json.dumps({"artifact_ids": [preview.artifact_id]}), None, "{}", old, old),
                )
            conn.execute(
                "INSERT INTO export_snapshots (workspace_id,export_id,run_id,manifest_json,manifest_sha256,created_at) VALUES (?,?,?,?,?,?)",
                ("ws", "e" * 32, "r" * 32, "{}", "h", old),
            )
        quarantine = artifacts / "quarantine" / "ws"
        quarantine.mkdir(parents=True)
        abandoned = quarantine / ("a" * 32)
        abandoned.write_bytes(b"old upload")
        import os
        os.utime(abandoned, (now.timestamp() - 48 * 3600, now.timestamp() - 48 * 3600))

        dry = OperationsService(storage, artifacts).enforce_retention(
            actor, now=now, dry_run=True,
        )
        assert dry["state"] == "dry_run"
        assert dry["expired_exports"] == 1
        assert dry["preview_artifacts"] == 1
        assert abandoned.exists()
        assert (artifacts / "ws" / "ws" / "artifacts" / referenced.artifact_id).exists()

        result = OperationsService(storage, artifacts).enforce_retention(actor, now=now)
        assert result["state"] == "completed"
        assert result["expired_exports"] == 1
        assert not abandoned.exists()
        expired = set(preview.artifact_id for preview in previews) - {
            preview.artifact_id for preview in previews if (artifacts / "ws" / "ws" / "artifacts" / preview.artifact_id).exists()
        }
        assert len(expired) == 1
        assert sum(
            (artifacts / "ws" / "ws" / "artifacts" / preview.artifact_id).exists()
            for preview in previews
        ) == 20
        assert (artifacts / "ws" / "ws" / "artifacts" / referenced.artifact_id).exists()
        assert storage.get_export_snapshot("e" * 32, "ws") is None
    finally:
        storage.close()

def test_readiness_checks_the_mounted_artifact_root(tmp_path):
    db = tmp_path / "readiness.db"
    storage = Storage(db)
    storage.create_schema()
    mount_parent = tmp_path / "mount-parent"
    artifact_root = mount_parent / "artifacts"
    artifact_root.mkdir(parents=True)
    mount_parent.chmod(0o555)
    try:
        result = OperationsService(storage, artifact_root).readiness("ws")
        assert result == {"status": "ready", "checks": {"database": "ok", "artifact_root": "ok"}}
    finally:
        mount_parent.chmod(0o755)
        storage.close()


def test_postgres_backup_restore_uses_dump_tools_and_keeps_install_keys_outside_archive(tmp_path, monkeypatch):
    db = tmp_path / "source.db"
    artifacts = tmp_path / "artifacts"
    storage = Storage(db)
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "--file" in command:
            output = command[command.index("--file") + 1]
            Path(output).write_bytes(b"pg-dump")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(operations_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(operations_module.subprocess, "run", fake_run)
    try:
        record = ArtifactStore(storage, artifacts, actor).put("ws", b"evidence", "text/plain")
        storage._is_postgres = True
        storage._db_path = "postgresql://eval_app:secret@example.test/eval_db"

        @contextmanager
        def local_workspace_transaction(_workspace_id):
            yield storage._conn

        monkeypatch.setattr(storage, "workspace_transaction", local_workspace_transaction)
        backup = tmp_path / "postgres-backup.zip"
        result = OperationsService(
            storage, artifacts,
            maintenance_database_url="postgresql://eval_maint:secret@example.test/eval_db",
        ).backup_postgres(actor, backup)
        assert result["state"] == "ready"
        assert result["storage"] == "postgresql"
        assert result["install_keys"] == "excluded_from_archive_restore_separately"
        assert commands[0][0] == "/usr/bin/pg_dump"
        assert "secret" not in commands[0]

        restored_artifacts = tmp_path / "restored-artifacts"
        restored = OperationsService.restore_postgres(
            backup, "postgresql://eval_app:secret@example.test/restored", restored_artifacts,
        )
        assert restored["state"] == "restored"
        assert restored["install_keys"] == "required_separately"
        assert commands[1][0] == "/usr/bin/pg_restore"
        assert (
            restored_artifacts / "ws" / "ws" / "artifacts" / record.artifact_id
        ).read_bytes() == b"evidence"
    finally:
        storage.close()


def test_postgres_command_strips_password_but_preserves_authority_separator():
    safe, environment = operations_module.OperationsService._safe_database_command(
        "postgresql://eval_maint:p%40ss@example.test:5432/eval_db?sslmode=require"
    )
    assert safe == "postgresql://eval_maint@example.test:5432/eval_db?sslmode=require"
    assert environment["PGPASSWORD"] == "p@ss"


def test_postgres_backup_requires_explicit_maintenance_authority(tmp_path):
    storage = Storage(tmp_path / "source.db")
    storage.create_schema()
    storage._is_postgres = True
    actor = Actor("owner", "ws", "owner")
    try:
        with pytest.raises(WorkflowError, match="PostgreSQL maintenance URL"):
            OperationsService(storage, tmp_path / "artifacts").backup_postgres(
                actor, tmp_path / "backup.zip",
            )
    finally:
        storage.close()
