import io
import json
import zipfile

from llm_agent_eval.ci_api import exit_code
from llm_agent_eval.exports import ExportService, _formula_safe


def test_exports_escape_formula_like_cells_and_ci_never_treats_unknown_as_pass():
    assert _formula_safe("=SUM(A1)").startswith("'")
    assert exit_code({"status": "complete", "metrics": [{"gate_status": "PASS", "no_ci": False}]}) == 0
    assert exit_code({"status": "complete", "metrics": [{"gate_status": "FAIL", "no_ci": False}]}) == 1
    assert exit_code({"status": "complete", "metrics": [{"gate_status": "NOT_APPLICABLE", "no_ci": False}]}) == 2
    assert exit_code({"status": "incomplete", "metrics": []}) == 2


def test_html_export_discloses_partial_and_provisional_limitations():
    html = ExportService._html_text({
        "run": {"status": "incomplete"},
        "metrics": [{
            "metric_id": "judge-quality",
            "aggregation_state": "PARTIAL",
            "provisional": True,
        }],
        "cases": [],
        "case_metrics": [],
    })

    assert "Partial results" in html
    assert "Provisional" in html
    assert "judge-quality" in html


def test_html_export_discloses_missing_retained_evidence():
    html = ExportService._html_text({
        "run": {"status": "complete"},
        "metrics": [],
        "cases": [],
        "case_metrics": [],
        "evidence": {"events": [], "missing_event_ids": ["expired-event"]},
    })

    assert "Evidence unavailable" in html
    assert "1 retained evidence reference(s)" in html


class _Event:
    def __init__(self, event_id, payload):
        self.event_id = event_id
        self.payload = payload

    def model_dump(self, *, mode):
        assert mode == "json"
        return {"event_id": self.event_id, "payload": self.payload}


class _Row:
    def __init__(self, evidence_event_ids):
        self.case_id = "case-1"
        self.metric_id = "metric-1"
        self.score_revision = 1
        self.status = "PASS"
        self.score = 1
        self.raw_value = 1
        self.evidence_event_ids = tuple(evidence_event_ids)
        self.evaluator_version = "evaluator-1"
        self.judge_binding = None
        self.is_authoritative = True
        self.on_retry_override = False
        self.overridden = False
        self.computed_at = None


class _Storage:
    def get_trace_events(self, **kwargs):
        assert kwargs["run_id"] == "run-1"
        return [_Event("event-1", {"content": "already redacted"})]


def test_evidence_bundle_contains_frozen_results_hashes_and_retained_evidence():
    manifest = {
        "schema_version": 1,
        "run": {"run_id": "run-1", "status": "complete"},
        "versions": {"version_refs": {"evaluation_version_id": "eval-1"}},
        "filters": {"case_ids": None, "metric_ids": None},
        "metrics": [],
        "cases": [{"case_id": "case-1", "case_key": "case", "status": "PASS", "classification": None, "error_category": None}],
        "case_metrics": [{
            "case_id": "case-1",
            "metric_id": "metric-1",
            "status": "PASS",
            "score": 1,
            "raw_value": 1,
            "evidence_event_ids": ["event-1"],
        }],
        "provenance": {"trace_redaction": "capture_time"},
        "evidence": {
            "events": [{"event_id": "event-1", "payload": {"content": "already redacted"}}],
            "missing_event_ids": [],
        },
    }

    bundle = ExportService._bundle_bytes(manifest)

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = archive.namelist()
        assert names[:3] == ["bundle-manifest.json", "manifest.json", "results.csv"]
        assert "report.html" in names
        evidence_names = [name for name in names if name.startswith("evidence/")]
        assert len(evidence_names) == 1
        bundle_manifest = json.loads(archive.read("bundle-manifest.json"))
        assert bundle_manifest["manifest_sha256"] == ExportService._manifest_sha256(manifest)
        assert bundle_manifest["files"]["manifest.json"]
        assert json.loads(archive.read(evidence_names[0]))["event_id"] == "event-1"


def test_manifest_captures_only_scoped_retained_evidence_and_missing_refs():
    service = ExportService(_Storage())
    service.storage.get_run = lambda run_id, workspace_id: type(
        "Run", (), {
            "run_id": "run-1", "status": "complete", "tier": "quick", "repeats": 1,
            "retry_max": 0, "project_id": "project-1", "created_at": None,
            "run_completed_at": None, "spec_json": json.dumps({"metrics": []}),
            "agent_version": {}, "run_manifest_ref": None, "world_config": {},
        },
    )()
    service.storage.get_run_metric_results = lambda run_id, workspace_id: []
    service.storage.list_cases = lambda run_id, workspace_id: []
    service.storage.get_case_scores = lambda **kwargs: [_Row(("event-1", "expired-event"))]

    manifest = service.snapshot("workspace-1", "run-1")["manifest"]

    assert [event["event_id"] for event in manifest["evidence"]["events"]] == ["event-1"]
    assert manifest["evidence"]["missing_event_ids"] == ["expired-event"]
