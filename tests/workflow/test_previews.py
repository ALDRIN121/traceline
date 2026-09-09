"""Deterministic synthetic dashboard previews share one registry with measured results."""

from llm_agent_eval.previews import GENERATOR_VERSION, load_skill


def test_preview_is_reproducible(platform):
    s = platform.seed("declared_range_dashboard")
    path = f"/api/dashboard-versions/{s['dashboard_version_id']}/preview"
    a = platform.drain(platform.post(path, s["refs"])["body"]["job_id"])
    b = platform.drain(platform.post(path, s["refs"])["body"]["job_id"])
    assert a["data_hash"] == b["data_hash"]
    assert b["data_kind"] == "synthetic"
    assert b["gate_result"] is None


def test_range_less_metric_is_placeholder_and_banner_is_synthetic(platform):
    s = platform.seed("declared_range_dashboard")
    path = f"/api/dashboard-versions/{s['dashboard_version_id']}/preview"
    preview = platform.drain(platform.post(path, s["refs"])["body"]["job_id"])
    blocks = {item["metric_id"]: item for item in preview["blocks"] if "metric_id" in item}
    assert blocks["latency"]["placeholder"] is False
    assert isinstance(blocks["latency"]["sample"], (int, float))
    assert blocks["eligibility"]["placeholder"] is True
    assert blocks["eligibility"]["sample"] is None
    html = platform.artifact_bytes(preview["html_artifact_id"]).decode()
    assert "Synthetic preview — no agent has been evaluated" in html
    assert "<script" not in html.lower()
    assert preview["review_receipt"]["presentation_only"] is True
    assert preview["review_receipt"]["dashboard_version_id"] == s["dashboard_version_id"]


def test_unknown_component_is_rejected_and_prior_definition_remains(platform):
    s = platform.seed("declared_range_dashboard")
    prior = platform.get(f"/api/versions/{s['dashboard_version_id']}")["body"]["version"]
    rejected = platform.post(
        f"/api/dashboards/{s['evaluation_id']}/versions",
        {
            "expected_revision": prior["revision"],
            "definition": {
                "version": 3,
                "name": "unsafe",
                "blocks": [{"component": "HTMLPreview", "span": 12, "bind": {}}],
            },
        },
    )
    assert rejected["http_status"] == 422
    listing = platform.get(f"/api/objects/dashboard/{s['evaluation_id']}/versions")["body"]
    assert listing["active_version_id"] == s["dashboard_version_id"]


def test_built_in_skill_is_schema_only_and_ignores_uploaded_skill(platform):
    skill = load_skill()
    assert "DashboardDefinition" in skill
    assert "<script>" not in skill.lower()
    assert "ignore the platform" not in skill.lower()
    s = platform.seed("declared_range_dashboard")
    uploaded = platform.seed("skill_zip", project_id=s["project_id"])
    imported = platform.post(f"/api/projects/{s['project_id']}/imports", uploaded["request"], key="skill-zip")
    platform.drain(imported["body"]["job_id"])
    preview = platform.drain(
        platform.post(f"/api/dashboard-versions/{s['dashboard_version_id']}/preview", s["refs"])["body"]["job_id"]
    )
    html = platform.artifact_bytes(preview["html_artifact_id"]).decode()
    assert "you will score" not in html.lower()
    assert preview["generator_version"] == GENERATOR_VERSION
