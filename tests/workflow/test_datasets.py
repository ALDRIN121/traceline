"""Dataset import maps fields, preserves provenance, and never invents gold labels."""

import json


MIXED_MAPPING = {
    "format": "jsonl",
    "case_id": "/case_id",
    "input": "/input",
    "expected": "/expected",
    "label_status": "/label_status",
    "tags": "/tags",
    "split": "/split",
    "fixture_state": "/fixture_state",
}
METRIC_REQUIREMENTS = [
    {"metric_id": "latency", "class": "health", "requires_label": False},
    {"metric_id": "accuracy", "class": "accuracy", "requires_label": True},
]


def test_inferred_is_not_gold(platform):
    s = platform.seed("mixed_label_dataset")
    report = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    assert report["label_coverage"]["approved"] == 2
    assert report["label_coverage"]["total"] == 4
    assert report["cases"][2]["label_status"] == "inferred"


def test_health_metrics_cover_unlabeled_accuracy_does_not(platform):
    s = platform.seed("mixed_label_dataset")
    report = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    assert report["eligibility"]["health"]["eligible_cases"] == 4
    assert report["eligibility"]["accuracy"]["eligible_cases"] == 2
    assert report["eligibility"]["accuracy"]["coverage"] == "2/4"
    assert report["cases"][3]["label_status"] == "missing"


def test_golden_values_are_absent_from_agent_request(platform):
    s = platform.seed("mixed_label_dataset")
    report = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    blob = json.dumps(report["cases"][0]["agent_request"])
    assert "shipped" not in blob
    assert report["cases"][0]["expected"]["answer"]["value"] == "shipped"
    assert report["cases"][0]["input"]["q"] == "where is order 1"


def test_csv_and_json_pointer_mapping_distinguish_null_from_missing(platform):
    csv_seed = platform.seed("csv_dataset")
    csv_report = csv_seed["report"]
    assert csv_report["cases"][0]["label_status"] == "approved"
    assert csv_report["cases"][1]["label_status"] == "missing"
    assert csv_report["cases"][1]["fields"]["expected"]["state"] == "missing"

    json_seed = platform.seed("null_vs_missing_dataset")
    rows = json_seed["report"]["cases"]
    assert rows[0]["fields"]["expected"]["state"] == "null"
    assert rows[1]["fields"]["expected"]["state"] == "missing"


def test_duplicate_identities_are_rejected_without_a_version(platform):
    s = platform.seed("duplicate_dataset")
    assert s["import"]["http_status"] == 422
    assert s["import"]["body"]["error"]["code"] == "duplicate_identity"


def test_explicit_exclusions_are_accounted_and_prior_version_is_immutable(platform):
    s = platform.seed("mixed_label_dataset")
    first = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    imported = platform.post(
        f"/api/projects/{s['project_id']}/datasets/imports",
        {
            "upload_id": s["upload_id"],
            "dataset_id": s["dataset_id"],
            "mapping": MIXED_MAPPING,
            "metric_requirements": METRIC_REQUIREMENTS,
        },
    )
    committed = platform.post(
        f"/api/datasets/{s['dataset_id']}/commit",
        {
            "report_id": imported["body"]["report_id"],
            "explicit_exclusions": ["c4"],
            "expected_revision": first["revision"],
        },
    )
    assert committed["http_status"] == 201
    second = platform.get(
        f"/api/datasets/{s['dataset_id']}/versions/{committed['body']['version_id']}"
    )["body"]
    assert [case["case_id"] for case in second["cases"]] == ["c1", "c2", "c3"]
    assert second["exclusions"] == [{"case_id": "c4", "reason": "explicit"}]
    assert second["label_coverage"]["total"] == 3
    unchanged = platform.get(f"/api/datasets/{s['dataset_id']}/versions/{s['version_id']}")["body"]
    assert [case["case_id"] for case in unchanged["cases"]] == [case["case_id"] for case in first["cases"]]
    assert unchanged["content_digest"] == first["content_digest"]


def test_oversized_case_input_is_reported_not_silently_dropped(platform):
    huge = "x" * (256 * 1024 + 1)
    payload = json.dumps({"case_id": "big", "input": {"q": huge}, "expected": {"answer": "no"}}) + "\n"
    project = platform.seed("empty_project")
    uploaded = platform.upload_bytes(payload.encode(), "application/x-ndjson")
    imported = platform.post(
        f"/api/projects/{project['project_id']}/datasets/imports",
        {
            "upload_id": uploaded,
            "mapping": MIXED_MAPPING,
            "metric_requirements": METRIC_REQUIREMENTS,
        },
    )
    assert imported["http_status"] == 422
    assert imported["body"]["error"]["code"] == "case_input_limit"
    assert "big" in json.dumps(imported["body"])
