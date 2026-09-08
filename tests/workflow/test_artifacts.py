"""Catch publication of partial/corrupt files and cross-tenant downloads."""

import os


def test_failed_artifact_attachment_leaves_no_ready_object(platform, monkeypatch):
    client = platform.clients["editor"]
    # Publication is a real filesystem boundary: failure must roll back metadata.
    def failed_publish(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", failed_publish)
    response = client.post("/api/artifacts", content=b"evidence", headers={"Content-Type": "text/plain"})
    assert response.status_code == 500
    listing = client.get("/api/artifacts")
    assert listing.status_code == 200
    assert listing.json()["artifacts"] == []
    assert not list((platform.root / "artifacts").rglob("*.stage"))


def test_artifact_checksum_scope_and_restart(platform):
    response = platform.clients["editor"].post("/api/artifacts", content=b"hello", headers={"Content-Type": "text/plain"})
    assert response.status_code == 201
    artifact = response.json()["artifact"]
    assert artifact["checksum"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert artifact["size_bytes"] == 5
    assert "path" not in artifact
    path = f"/api/artifacts/{artifact['artifact_id']}"
    assert platform.clients["other_editor"].get(path).status_code == 404
    assert platform.clients["editor"].get("/api/artifacts/guessed").status_code == 404
    platform.restart()
    assert platform.clients["editor"].get(path).content == b"hello"
