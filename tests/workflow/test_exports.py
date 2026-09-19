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
