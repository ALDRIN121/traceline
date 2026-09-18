from __future__ import annotations

from llm_agent_eval.auth import Actor
from llm_agent_eval.operations import OperationsService
from llm_agent_eval.storage import Storage


def test_sqlite_backup_restore_verifies_manifest_and_artifacts(tmp_path):
    db = tmp_path / "source.db"
    artifacts = tmp_path / "artifacts"
    storage = Storage(db)
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    try:
        from llm_agent_eval.artifacts import ArtifactStore
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
