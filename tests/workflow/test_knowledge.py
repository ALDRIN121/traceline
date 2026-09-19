"""Evidence-backed discovery never claims execution, and confirmations version."""


def test_static_findings_do_not_claim_execution(platform):
    s = platform.seed("dynamic_tool_repo")
    report = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    assert report["verification_id"] is None
    assert all(f.get("observed_in_attempt_id") is None for f in report["facts"])
    assert report["needs_entrypoint_declaration"] is True


def test_runtime_dict_tools_are_inferred_with_file_locations(platform):
    s = platform.seed("dynamic_tool_repo")
    report = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    tools = {fact["name"]: fact for fact in report["facts"] if fact["kind"] == "tool"}
    assert set(tools) == {"check_refund_eligibility", "lookup_order_status", "issue_refund"}
    for fact in tools.values():
        assert fact["status"] == "inferred"
        assert fact["confidence"] == 0.3
        assert fact["evidence"][0]["path"] == "src/support/tools.py"
        assert fact["evidence"][0]["line_start"] >= 1


def test_readme_instructions_are_not_static_truth(platform):
    s = platform.seed("dynamic_tool_repo")
    report = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    blob = str(report["facts"]) + report.get("markdown_report", "")
    assert "already verified execution" not in blob.lower()
    assert report["verification_id"] is None


def test_confirm_then_source_update_marks_review_needed_and_survives_restart(platform):
    s = platform.seed("dynamic_tool_repo")
    report = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    pending = report["pending_questions"]
    assert pending
    fact = next(item for item in report["facts"] if item["name"] == "check_refund_eligibility")
    confirmed = platform.post(
        f"/api/projects/{s['project_id']}/knowledge/confirm",
        {
            "expected_revision": report["revision"],
            "report_id": report["report_id"],
            "corrections": [{
                "fact_id": fact["fact_id"],
                "status": "user_confirmed",
                "summary": "The agent checks refund eligibility before issuing a refund.",
            }],
        },
    )
    assert confirmed["http_status"] == 201
    assert confirmed["body"]["facts"]
    assert any(
        item["fact_id"] == fact["fact_id"] and item["status"] == "user_confirmed"
        for item in confirmed["body"]["facts"]
    )
    corrected = next(item for item in confirmed["body"]["facts"] if item["fact_id"] == fact["fact_id"])
    assert corrected["summary"] == "The agent checks refund eligibility before issuing a refund."
    assert corrected["original_summary"] == fact.get("summary", "")

    updated = platform.seed("dynamic_tool_repo_updated", project_id=s["project_id"])
    imported = platform.post(
        f"/api/projects/{s['project_id']}/imports", updated["request"], key="knowledge-source-update"
    )
    platform.drain(imported["body"]["job_id"])
    refreshed = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    refund = next(item for item in refreshed["facts"] if item["fact_id"] == fact["fact_id"])
    assert refund["status"] == "review_needed"
    assert refreshed["review_needed"] is True
    assert refreshed["pending_questions"][0]["question_id"] == pending[0]["question_id"]
    assert refreshed["pending_questions"][0]["text"] == pending[0]["text"]

    platform.restart()
    recovered = platform.get(f"/api/projects/{s['project_id']}/knowledge")["body"]
    assert recovered["pending_questions"][0] == pending[0]
    assert recovered["markdown_report"]
    assert any(item["status"] == "review_needed" for item in recovered["facts"])


def test_retrieval_cites_nested_source_within_budget(platform):
    s = platform.seed("dynamic_tool_repo")
    response = platform.post(
        f"/api/projects/{s['project_id']}/knowledge/retrieve",
        {"query": "refund eligibility", "budget": 3},
    )
    assert response["http_status"] == 200
    snippets = response["body"]["snippets"]
    assert 1 <= len(snippets) <= 3
    assert all(item["path"].endswith(".py") for item in snippets)
    assert any("refund" in item["text"].lower() for item in snippets)
    assert all("source_version_id" in item for item in snippets)
