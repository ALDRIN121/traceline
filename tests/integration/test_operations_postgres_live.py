"""Opt-in PostgreSQL backup/restore rehearsal against real dump tools."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import zipfile
import uuid

import pytest

from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.operations import OperationsService
from llm_agent_eval.storage import Storage


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
