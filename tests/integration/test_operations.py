from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from llm_agent_eval.auth import Actor
from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.operations import OperationsService
from llm_agent_eval.storage import Storage


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
        assert (restored_artifacts / record.artifact_id).read_bytes() == b"evidence"
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
