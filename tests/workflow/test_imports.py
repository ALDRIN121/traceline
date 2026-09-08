"""Workflow contracts for immutable, non-executing source import."""

import pytest
import zipfile
from io import BytesIO


@pytest.mark.parametrize("scenario", ["valid_zip", "reference_broken_zip"])
def test_zip_import_persists_a_source_version_or_parse_diagnostics(platform, scenario):
    s = platform.seed(scenario)
    created = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"], key=f"{scenario}-one")

    assert created["http_status"] == 202
    result = platform.drain(created["body"]["job_id"])
    if scenario == "valid_zip":
        assert result["status"] == "completed"
        assert result["result"]["source_version_id"]
        assert result["result"]["readiness"] == "blocked"
    else:
        assert result["status"] == "completed"
        assert result["result"]["source_version_id"]
        assert result["result"]["readiness"] == "blocked"
        assert result["result"]["diagnostics"][0]["code"] == "parse_failed"


def test_sanitized_snapshot_excludes_dotenv_and_hardcoded_secret_from_readable_artifact(platform):
    s = platform.seed("valid_zip")
    created = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"], key="safe-snapshot")
    result = platform.drain(created["body"]["job_id"])
    version = platform.get(f"/api/versions/{result['result']['source_version_id']}")["body"]["version"]

    snapshot = platform.artifact_bytes(version["content"]["artifact_ids"][0])
    with zipfile.ZipFile(BytesIO(snapshot)) as archive:
        assert archive.namelist() == ["agent.py"]
        assert b"sk-test-do-not-persist-0001" not in snapshot


def test_raw_upload_is_quarantine_only_and_never_enters_public_manifest(platform):
    s = platform.seed("valid_zip")
    raw_upload_id = s["request"]["upload_id"]
    assert raw_upload_id not in [item["artifact_id"] for item in platform.get("/api/artifacts")["body"]["artifacts"]]
    assert platform.get(f"/api/artifacts/{raw_upload_id}")["http_status"] == 404

    created = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"], key="quarantine-only")
    result = platform.drain(created["body"]["job_id"])
    version = platform.get(f"/api/versions/{result['result']['source_version_id']}")["body"]["version"]
    assert raw_upload_id not in str(version["content"])


def test_public_import_drain_route_executes_the_requested_queued_job(platform):
    s = platform.seed("valid_zip")
    created = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"], key="public-drain")
    response = platform.post(f"/api/import-jobs/{created['body']['job_id']}/drain", {})
    assert response["http_status"] == 200
    assert response["body"]["status"] == "completed"


@pytest.mark.parametrize(("scenario", "code"), [
    ("zip_traversal", "unsafe_archive_path"),
    ("zip_case_collision", "archive_name_collision"),
    ("zip_expansion_limit", "archive_expansion_limit"),
    ("nested_archive", "nested_archive"),
    ("missing_git_ref", "git_ref_not_found"),
    ("unapproved_git_host", "git_source_rejected"),
])
def test_unsafe_or_unresolved_import_has_no_source_version(platform, scenario, code):
    s = platform.seed(scenario)
    created = platform.post(f"/api/projects/{s['project_id']}/imports", s["request"], key=f"{scenario}-one")

    result = platform.drain(created["body"]["job_id"])

    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert result.get("source_version_id") is None
