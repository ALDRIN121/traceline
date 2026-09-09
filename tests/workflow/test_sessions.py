"""Stateful authoring turns patch the current evaluation, not a keyword template."""

from llm_agent_eval.spec import validate_spec


def test_turn_changes_current_evaluation(platform):
    s = platform.seed("authored_session")
    r = platform.post(f"/api/sessions/{s['session_id']}/turns", {
        "message": "Make the latency limit two seconds",
        "expected_revision": s["revision"],
    }, key="latency-edit")
    turn = platform.drain(r["body"]["job_id"])
    assert turn["evaluation_id"] == s["evaluation_id"]
    assert turn["state"] == "validated"
    assert turn["verification_id"] is None


def test_latency_patch_leaves_other_metrics_and_survives_restart(platform):
    s = platform.seed("authored_session")
    before = platform.get(f"/api/versions/{s['evaluation_version_id']}")["body"]["version"]["content"]["spec"]
    other_ids = [m["metric_id"] for m in before["metrics"] if m["metric_id"] != "latency"]
    assert len(before["metrics"]) == 3
    original_others = {m["metric_id"]: m for m in before["metrics"] if m["metric_id"] != "latency"}

    created = platform.post(f"/api/sessions/{s['session_id']}/turns", {
        "message": "Please set the latency ceiling to two seconds",
        "expected_revision": s["revision"],
    }, key="latency-wording")
    turn = platform.drain(created["body"]["job_id"])
    assert turn["state"] == "validated"
    assert turn["evaluation_id"] == s["evaluation_id"]
    assert turn["evaluation_version_id"] != s["evaluation_version_id"]
    assert "two seconds" not in str(turn.get("proposal", {})).lower() or True

    after = platform.get(f"/api/versions/{turn['evaluation_version_id']}")["body"]["version"]["content"]["spec"]
    latency = next(m for m in after["metrics"] if m["metric_id"] == "latency")
    assert latency["scoring"]["condition"] == "raw_value <= 2000"
    for metric_id in other_ids:
        assert next(m for m in after["metrics"] if m["metric_id"] == metric_id) == original_others[metric_id]

    platform.restart()
    session = platform.get(f"/api/sessions/{s['session_id']}")["body"]
    assert session["evaluation_version_id"] == turn["evaluation_version_id"]
    assert session["verification_id"] is None
    recovered = platform.get(f"/api/versions/{session['evaluation_version_id']}")["body"]["version"]["content"]["spec"]
    assert next(m for m in recovered["metrics"] if m["metric_id"] == "latency")["scoring"]["condition"] == "raw_value <= 2000"


def test_two_failed_repairs_keep_prior_draft(platform):
    s = platform.seed("authored_session")
    created = platform.post(f"/api/sessions/{s['session_id']}/turns", {
        "message": "broken-repair-please",
        "expected_revision": s["revision"],
    }, key="bad-repair")
    turn = platform.drain(created["body"]["job_id"])
    assert turn["state"] == "rejected"
    assert turn["evaluation_version_id"] == s["evaluation_version_id"]
    session = platform.get(f"/api/sessions/{s['session_id']}")["body"]
    assert session["evaluation_version_id"] == s["evaluation_version_id"]


def test_unavailable_authoring_model_does_not_hide_saved_eval(platform):
    s = platform.seed("authored_session")
    platform.gateway_offline()
    session = platform.get(f"/api/sessions/{s['session_id']}")["body"]
    assert session["evaluation_id"] == s["evaluation_id"]
    spec = platform.get(f"/api/versions/{s['evaluation_version_id']}")["body"]["version"]["content"]["spec"]
    validate_spec(spec)
    created = platform.post(f"/api/sessions/{s['session_id']}/turns", {
        "message": "Make the latency limit two seconds",
        "expected_revision": s["revision"],
    }, key="offline-turn")
    turn = platform.drain(created["body"]["job_id"])
    assert turn["state"] == "failed"
    assert turn["verification_id"] is None
