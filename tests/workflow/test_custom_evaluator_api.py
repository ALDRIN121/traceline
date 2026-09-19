from __future__ import annotations

import io
import zipfile


def test_custom_evaluator_api_requires_review_before_it_can_be_selected(platform):
    project_id = platform.seed("empty_project")["project_id"]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("evaluate.py", "# reviewed")
    uploaded = platform.clients["editor"].post(
        "/api/artifacts", content=archive.getvalue(),
        headers={"content-type": "application/zip"},
    )
    assert uploaded.status_code == 201
    artifact_id = uploaded.json()["artifact"]["artifact_id"]
    definition = {
        "name": "policy",
        "artifact_ids": [artifact_id],
        "image_digest": "sha256:" + "a" * 64,
        "entrypoint": ["/usr/bin/python", "/source/evaluate.py"],
        "score_range": [0, 1],
        "pass_threshold": 0.5,
    }
    created = platform.post(
        f"/api/projects/{project_id}/evaluator-versions",
        {"expected_revision": 0, "definition": definition},
    )
    assert created["http_status"] == 201
    version_id = created["body"]["version"]["version_id"]
    assert created["body"]["version"]["content"]["review"]["state"] == "draft"

    approved = platform.post(f"/api/evaluator-versions/{version_id}/approve", {})
    assert approved["http_status"] == 201
    assert approved["body"]["version"]["content"]["review"]["state"] == "approved"
    listed = platform.get(f"/api/projects/{project_id}/evaluator-versions")
    assert listed["http_status"] == 200
    assert listed["body"]["active_revision"] == 2

