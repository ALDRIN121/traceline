"""Opt-in PostgreSQL backup/restore rehearsal against real dump tools."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import zipfile
import uuid

import pytest

from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.operations import OperationsService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


@pytest.mark.integration
def test_live_postgres_backup_restore_preserves_workspace_data(tmp_path: Path):
    source_url = os.environ.get("TEST_DATABASE_URL")
    maintenance_url = os.environ.get("TEST_DATABASE_MAINTENANCE_URL")
    restore_url = os.environ.get("TEST_DATABASE_RESTORE_URL")
    if not source_url or not maintenance_url or not restore_url:
        pytest.skip(
            "TEST_DATABASE_URL, TEST_DATABASE_MAINTENANCE_URL, and "
            "TEST_DATABASE_RESTORE_URL are required"
        )
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("PostgreSQL client tools are required")

    workspace = f"backup_{uuid.uuid4().hex}"
    actor = Actor("backup-owner", workspace, "owner")
    source = Storage(source_url)
    source.create_schema()
    artifact_root = tmp_path / "source-artifacts"
    try:
        project = source.create_project(workspace_id=workspace, name="backup rehearsal")
        record = ArtifactStore(source, artifact_root, actor).put(
            workspace, b"authoritative evidence", "text/plain",
        )
        backup = tmp_path / "workspace-backup.zip"
        service = OperationsService(
            source, artifact_root, maintenance_database_url=maintenance_url,
        )
        result = service.backup_workspace(actor, backup)
        assert result["state"] == "ready"
        assert backup.is_file()
        with zipfile.ZipFile(backup) as archive:
            names = set(archive.namelist())
            assert "manifest.json" in names
            assert f"artifacts/{record.artifact_id}" in names
            assert maintenance_url.encode() not in archive.read("manifest.json")

        restored_root = tmp_path / "restored-artifacts"
        restored = OperationsService.restore_postgres(
            backup, restore_url, restored_root,
        )
        assert restored["state"] == "restored"
        restored_storage = Storage(restore_url)
        try:
            restored_storage.create_schema()
            restored_project = restored_storage.get_project(project.project_id, workspace)
            assert restored_project is not None
            restored_bytes = ArtifactStore(
                restored_storage, restored_root, actor,
            ).get(workspace, record.artifact_id)
            assert restored_bytes == b"authoritative evidence"
        finally:
            restored_storage.close()
    finally:
        source.close()


@pytest.mark.integration
def test_live_postgres_retention_preserves_version_links_and_recent_previews(tmp_path: Path):
    database_url = os.environ.get("TEST_DATABASE_WORKER_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_WORKER_URL is required")

    workspace = f"retention_{uuid.uuid4().hex}"
    actor = Actor("retention-owner", workspace, "owner")
    storage = Storage(database_url)
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    old = (now - timedelta(days=31)).isoformat()
    artifact_root = tmp_path / "retention-artifacts"
    try:
        storage.create_schema()
        project = storage.create_project(workspace_id=workspace, name="retention rehearsal")
        linked = ArtifactStore(storage, artifact_root, actor).put(
            workspace, b"version-linked", "application/octet-stream",
        )
        VersionStore(storage).create(
            "dashboard", project.project_id, {"artifact_ids": [linked.artifact_id]}, 0, actor,
        )
        previews = [
            ArtifactStore(storage, artifact_root, actor).put(
                workspace, f"preview-{index}".encode(), "text/html",
            )
            for index in range(21)
        ]
        with storage.workspace_transaction(workspace) as conn:
            for index, preview in enumerate(previews):
                conn.execute(
                    "INSERT INTO jobs (workspace_id,job_id,command_json,command_digest,idempotency_key,status,attempts,max_attempts,fence,lease_worker_id,lease_expires_at,cancellation_requested,result_json,error_json,command_redaction_json,result_redaction_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'completed',1,1,1,NULL,NULL,0,?,?,?,NULL,?,?)",
                    (
                        workspace, f"{index + 1:032x}",
                        json.dumps({"kind": "dashboard_preview"}), "digest", f"preview-{index}",
                        json.dumps({"artifact_ids": [preview.artifact_id]}), None, "{}", old, old,
                    ),
                )
            conn.execute(
                "INSERT INTO export_snapshots (workspace_id,export_id,run_id,manifest_json,manifest_sha256,created_at) VALUES (?,?,?,?,?,?)",
                (workspace, "e" * 32, "r" * 32, "{}", "hash", old),
            )
        quarantine = artifact_root / "quarantine" / workspace
        quarantine.mkdir(parents=True)
        abandoned = quarantine / ("a" * 32)
        abandoned.write_bytes(b"abandoned")
        old_timestamp = (now - timedelta(hours=48)).timestamp()
        os.utime(abandoned, (old_timestamp, old_timestamp))

        dry = OperationsService(storage, artifact_root).enforce_retention(
            actor, now=now, dry_run=True,
        )
        assert dry["expired_exports"] == 1
        assert dry["preview_artifacts"] == 1

        result = OperationsService(storage, artifact_root).enforce_retention(actor, now=now)
        assert result["expired_exports"] == 1
        assert result["preview_artifacts"] == 1
        assert not abandoned.exists()
        assert ArtifactStore(storage, artifact_root, actor).get(workspace, linked.artifact_id) == b"version-linked"
        assert storage.get_export_snapshot("e" * 32, workspace) is None
        assert sum(
            (artifact_root / "ws" / workspace / "artifacts" / preview.artifact_id).exists()
            for preview in previews
        ) == 20
    finally:
        storage.close()
