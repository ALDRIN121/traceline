"""Protected records cannot bypass dedicated validation via generic writes."""

import pytest


@pytest.mark.parametrize("kind", ["evaluator", "model_profile", "model_selection", "judge_rubric"])
def test_generic_version_post_rejects_protected_kinds_without_publishing(platform, kind):
    project_id = platform.seed("empty_project")["project_id"]
    path = f"/api/objects/{kind}/{project_id}/versions"

    response = platform.post(path, {"expected_revision": 0, "content": {}})

    assert response["http_status"] == 422
    assert response["body"]["error"]["code"] == "protected_version_kind"
    listing = platform.get(path)["body"]
    assert listing["versions"] == []
    assert listing["active_revision"] == 0
    assert listing["active_version_id"] is None


def test_generic_source_version_creation_still_publishes(platform):
    project_id = platform.seed("empty_project")["project_id"]
    path = f"/api/objects/source/{project_id}/versions"
    content = {"readiness": "blocked"}

    response = platform.post(path, {"expected_revision": 0, "content": content})

    assert response["http_status"] == 201
    version = response["body"]["version"]
    assert version["content"] == content
    assert platform.get(path)["body"]["active_version_id"] == version["version_id"]
