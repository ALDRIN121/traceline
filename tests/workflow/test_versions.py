"""Catch lost edits, tenant confusion and non-durable authoring state."""

from llm_agent_eval.dashboard import DEFAULT_DEFINITION


def test_stale_edit_is_conflict(platform):
    scenario = platform.seed("two_workspace_projects")
    path = f"/api/projects/{scenario['project_id']}/knowledge/versions"
    body = {"expected_revision": 0, "facts": [], "confirmed": False}
    first = platform.post(path, body)
    assert first["http_status"] == 201
    second = platform.post(path, body)
    assert second["http_status"] == 409
    assert second["body"]["error"]["code"] == "conflict"
    assert platform.get(path)["body"]["active_revision"] == 1


def test_cross_workspace_cannot_read_write_or_spoof_workspace(platform):
    scenario = platform.seed("two_workspace_projects")
    path = f"/api/projects/{scenario['project_id']}/knowledge/versions"
    created = platform.post(path, {"expected_revision": 0, "facts": [{"name": "refund"}], "confirmed": False})
    assert created["http_status"] == 201
    version_path = f"/api/versions/{created['body']['version']['version_id']}"
    assert platform.get(version_path, actor="other_editor")["http_status"] == 404
    assert platform.get(path, actor="other_editor")["http_status"] == 404
    assert platform.post(path, {"expected_revision": 1, "facts": [], "confirmed": False}, actor="other_editor")["http_status"] == 404
    assert platform.post(path, {"expected_revision": 1, "facts": [], "confirmed": False, "workspace_id": "workspace_b"})["http_status"] == 422


def test_restart_recovers_immutable_version_and_active_pointer(platform):
    scenario = platform.seed("two_workspace_projects")
    path = f"/api/projects/{scenario['project_id']}/knowledge/versions"
    first = platform.post(path, {"expected_revision": 0, "facts": [{"name": "original"}], "confirmed": False})
    assert first["http_status"] == 201
    first = first["body"]["version"]
    second = platform.post(path, {"expected_revision": 1, "facts": [{"name": "revised"}], "confirmed": True})
    assert second["http_status"] == 201
    second = second["body"]["version"]
    platform.restart()
    assert platform.get(f"/api/versions/{first['version_id']}")["body"]["version"] == first
    recovered = platform.get(path)["body"]
    assert recovered["active_version_id"] == second["version_id"]
    assert recovered["active_revision"] == 2
    assert second["previous_version_id"] == first["version_id"]
    assert second["content_digest"] != first["content_digest"]


def test_legacy_definitions_are_snapshotted_or_quarantined_without_rewriting_them(platform):
    scenario = platform.seed("two_workspace_projects")
    valid = platform.clients["editor"].post(
        "/api/harness/chat", json={"message": "Analyze fixtures/sample-agent"}
    )
    assert valid.status_code == 200
    migrated_legacy = platform.store.create_custom_eval(
        workspace_id="workspace_a",
        name="valid legacy definition",
        spec=valid.json()["spec_data"],
        dashboard=DEFAULT_DEFINITION,
        project_id=scenario["project_id"],
    )
    invalid = platform.store.create_custom_eval(
        workspace_id="workspace_a",
        name="incomplete legacy definition",
        spec={"spec_version": "not-valid"},
        dashboard={"version": 0},
        project_id=scenario["project_id"],
    )

    response = platform.clients["editor"].post("/api/definitions/migrate")
    assert response.status_code == 200
    migrated = {(row["eval_id"], row["kind"]): row for row in response.json()["definitions"]}
    assert migrated[(migrated_legacy.eval_id, "evaluation")]["state"] == "migrated"
    assert migrated[(migrated_legacy.eval_id, "dashboard")]["state"] == "migrated"
    assert migrated[(invalid.eval_id, "evaluation")]["state"] == "quarantined"
    assert migrated[(invalid.eval_id, "dashboard")]["state"] == "quarantined"
    assert platform.store.get_custom_eval(invalid.eval_id, "workspace_a").spec == {"spec_version": "not-valid"}
