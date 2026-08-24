"""Tests for the FastAPI interaction surface (harness §17).

Covers §17.4's endpoints (create/start/cancel/status/progress/traces/
revisions), §17.6 idempotent creation (replay vs conflict), §17.7
cancellation (request not promise, worker confirmation), §17.8 SSE progress
(terminates with a terminal run.state), §17.9 cursor pagination, and §17.10's
error envelope. Runs execute in the API's background worker threads against
the fake agent fixture — the same fixture the engine tests use.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.engine import Engine
from llm_agent_eval.lifecycle import SmokeState
from llm_agent_eval.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = [sys.executable, str(FIXTURES / "fake_agent.py")]
WORKSPACE = "ws1"


def make_spec(n_cases=2, *, metric_overrides=None):
    cases = [
        {"case_id": f"c{i}", "name": f"case {i}", "input": {"order_id": str(i)}}
        for i in range(n_cases)
    ]
    metric = {
        "metric_id": "refund_amount",
        "name": "refund amount correct",
        "type": "scalar",
        "target": {
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        "evaluator": {"type": "numeric", "expected": 49.99, "tolerance": 0},
        "scoring": {"type": "binary", "range": [0, 1],
                    "condition": "raw_value == 49.99"},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "gate": {"min": 1.0},
        "provisional": False,
    }
    if metric_overrides:
        metric.update(metric_overrides)
    return {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "run_tier": "quick",
        "cases": cases,
        "metrics": [metric],
    }


@pytest.fixture()
def client(tmp_path):
    db = Storage(tmp_path / "eval.db")
    db.create_schema()
    engine = Engine(db, work_root=tmp_path / "work")
    app = create_app(storage=db, engine=engine, workspace_id=WORKSPACE)
    with TestClient(app) as c:
        yield c, engine, db
    db.close()


def create_run(client, spec=None, **kwargs):
    """POST /runs with the fake agent as the entrypoint (a run needs an
    entrypoint or an EVALUABLE project); pass tier=None to exercise the
    tier-required path."""
    body = {"spec": spec or make_spec()}
    body.setdefault("entrypoint", AGENT)
    body.setdefault("tier", "quick")
    body.update(kwargs)
    return client.post("/runs", json=body)


def wait_terminal(client, run_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["terminal"]:
            return data
        time.sleep(0.1)
    pytest.fail(f"run {run_id} did not reach a terminal state in time")


def run_to_complete(client, n_cases=2, **body_kwargs):
    """Create + start a run against the fake agent and wait for terminal."""
    created = create_run(client, entrypoint=AGENT, tier="quick", **body_kwargs)
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["run_id"]
    started = client.post(f"/runs/{run_id}/start")
    assert started.status_code == 202, started.text
    return run_id, wait_terminal(client, run_id)


class TestHealthAndEnvelope:
    def test_health(self, client):
        c, _, _ = client
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_error_envelope(self, client):
        c, _, _ = client
        resp = c.get("/runs/nope")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "not_found"
        assert "correlation_id" in body["error"]
        # Correlation id round-trips on the response header.
        assert resp.headers.get("x-correlation-id") == body["error"]["correlation_id"]

    def test_malformed_cursor(self, client):
        c, _, _ = client
        resp = c.get("/runs", params={"cursor": "not-base64!"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


class TestCreateRun:
    def test_create_and_get(self, client):
        c, _, _ = client
        resp = create_run(c)
        assert resp.status_code == 201
        body = resp.json()
        assert body["state"] == "created"
        assert body["idempotent_replay"] is False
        run_id = body["run"]["run_id"]
        assert body["run"]["status"] == "draft"
        assert body["run"]["spec"]["name"] == "refund-suite"

        got = c.get(f"/runs/{run_id}")
        assert got.status_code == 200
        data = got.json()
        assert data["terminal"] is False
        assert len(data["cases"]) == 2
        assert len(data["metrics"]) == 0  # nothing aggregated yet
        assert data["revisions"] == []

    def test_create_validation_error(self, client):
        c, _, _ = client
        spec = make_spec()
        del spec["metrics"][0]["target"]["on_missing"]
        resp = create_run(c, spec=spec)
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["details"]["problems"]

    def test_create_tier_violation(self, client):
        c, _, _ = client
        resp = create_run(c, spec=make_spec(n_cases=25), tier="quick")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_create_requires_tier(self, client):
        c, _, _ = client
        spec = make_spec()
        spec.pop("run_tier", None)
        resp = create_run(c, spec=spec, tier=None)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_project_not_evaluable(self, client):
        c, engine, _ = client
        project = engine.create_project(WORKSPACE, "refund-agent", entrypoint=AGENT)
        resp = create_run(
            c, project_id=project.project_id, tier="quick", entrypoint=AGENT
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "project_not_evaluable"
        assert body["error"]["details"]["current_lifecycle_state"] == SmokeState.INGESTED.value

    def test_project_evaluable_gate(self, client):
        c, engine, _ = client
        project = engine.create_project(WORKSPACE, "refund-agent", entrypoint=AGENT)
        engine.prepare_project(project.project_id, WORKSPACE, entrypoint=AGENT)
        attempt = engine.smoke(project.project_id, WORKSPACE)
        assert attempt.state == SmokeState.SMOKE_PASSED.value
        engine.advance_project(project.project_id, WORKSPACE, SmokeState.EVALUABLE)
        resp = create_run(c, project_id=project.project_id, tier="quick")
        assert resp.status_code == 201
        assert resp.json()["run"]["project_id"] == project.project_id


class TestIdempotency:
    def test_replay_returns_original(self, client):
        c, _, _ = client
        first = create_run(c, idempotency_key="k1", entrypoint=AGENT, tier="quick")
        assert first.status_code == 201
        second = create_run(c, idempotency_key="k1", entrypoint=AGENT, tier="quick")
        assert second.status_code == 200
        body = second.json()
        assert body["idempotent_replay"] is True
        assert body["run"]["run_id"] == first.json()["run"]["run_id"]
        # One run in the store.
        listed = c.get("/runs").json()
        assert len(listed["runs"]) == 1

    def test_header_key_wins_over_body(self, client):
        c, _, _ = client
        resp = c.post(
            "/runs",
            json={"spec": make_spec(), "idempotency_key": "body-key",
                  "entrypoint": AGENT, "tier": "quick"},
            headers={"Idempotency-Key": "header-key"},
        )
        assert resp.status_code == 201
        assert resp.json()["run"]["idempotency_key"] == "header-key"

    def test_conflict_different_payload(self, client):
        c, _, _ = client
        assert create_run(c, idempotency_key="k2").status_code == 201
        resp = create_run(c, idempotency_key="k2", spec=make_spec(n_cases=3))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "idempotency_conflict"


class TestStartCancel:
    def test_start_and_complete(self, client):
        c, _, _ = client
        run_id, final = run_to_complete(c)
        assert final["run"]["status"] == "complete"
        assert final["terminal"] is True
        for case in final["cases"]:
            assert case["status"] == "completed"
            assert case["metrics"][0]["status"] == "PASS"
        assert final["metrics"][0]["aggregation_state"] == "COMPLETE"
        assert final["metrics"][0]["gate_status"] == "PASS"

    def test_start_terminal_run_conflicts(self, client):
        c, _, _ = client
        run_id, _ = run_to_complete(c)
        resp = c.post(f"/runs/{run_id}/start")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "state_conflict"

    def test_start_missing_run(self, client):
        c, _, _ = client
        resp = c.post("/runs/ghost/start")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_cancel_mid_run(self, client, monkeypatch):
        """§17.7: cancel during execution returns pending_cancel; the worker
        confirms by landing the run in cancelled with the unrun case CANCELLED
        as evidence and PARTIAL aggregation."""
        monkeypatch.setenv("FAKE_AGENT_MODE", "sleep")
        monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "3")
        c, _, _ = client
        created = create_run(c, entrypoint=AGENT, tier="quick", retry_max=0)
        run_id = created.json()["run"]["run_id"]
        assert c.post(f"/runs/{run_id}/start").status_code == 202
        resp = c.post(f"/runs/{run_id}/cancel")
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state in ("pending_cancel", "cancelled")
        final = wait_terminal(c, run_id, timeout=30.0)
        assert final["run"]["status"] == "cancelled"
        statuses = {case["case_id"]: case["status"] for case in final["cases"]}
        assert any(s == "cancelled" for s in statuses.values())
        assert final["metrics"][0]["aggregation_state"] == "PARTIAL"

    def test_cancel_before_start(self, client):
        c, _, _ = client
        created = create_run(c)
        run_id = created.json()["run"]["run_id"]
        resp = c.post(f"/runs/{run_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["state"] == "cancelled"
        got = c.get(f"/runs/{run_id}").json()
        assert got["run"]["status"] == "cancelled"
        assert got["terminal"] is True

    def test_cancel_terminal_run(self, client):
        c, _, _ = client
        run_id, _ = run_to_complete(c)
        resp = c.post(f"/runs/{run_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["state"] == "already_terminal"


class TestSseProgress:
    def test_sse_terminates_with_terminal_event(self, client):
        """§17.8: the stream replays the snapshot on connect, emits per-case
        and per-metric events, and ends with a terminal run.state."""
        c, _, _ = client
        created = create_run(c, entrypoint=AGENT, tier="quick")
        run_id = created.json()["run"]["run_id"]
        assert c.post(f"/runs/{run_id}/start").status_code == 202

        pairs = []
        current = None
        with c.stream("GET", f"/runs/{run_id}/progress") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    current = line[len("event: "):]
                elif line.startswith("data: "):
                    pairs.append((current, json.loads(line[len("data: "):])))

        events = [e for e, _ in pairs]
        assert "case.completed" in events
        assert "metric.updated" in events
        last_event, last_data = pairs[-1]
        assert last_event == "run.state"
        assert last_data["terminal"] is True
        assert last_data["status"] == "complete"
        # The snapshot's first run.state is never terminal.
        assert pairs[0][0] == "run.state"
        assert pairs[0][1].get("terminal") is None


class TestTraces:
    def test_case_traces(self, client):
        c, _, _ = client
        run_id, _ = run_to_complete(c)
        resp = c.get(f"/runs/{run_id}/traces/c0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_count"] == 3
        types = [e["type"] for e in body["events"]]
        assert types == ["tool_call", "tool_result", "llm_response"]
        # Event ids are namespaced per attempt (the fixture's trace contract).
        assert len({e["event_id"] for e in body["events"]}) == 3

    def test_traces_missing_case(self, client):
        c, _, _ = client
        created = create_run(c)
        run_id = created.json()["run"]["run_id"]
        resp = c.get(f"/runs/{run_id}/traces/ghost")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_traces_missing_run(self, client):
        c, _, _ = client
        resp = c.get("/runs/ghost/traces/c0")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestRevisions:
    def test_override_stored_alongside_machine_score(self, client):
        """§13A.1.2: the override is additive — the machine row is untouched
        (still revision 1, flagged overridden) and the response carries both."""
        c, _, _ = client
        run_id, final = run_to_complete(c)
        machine = final["cases"][0]["metrics"][0]
        assert machine["score_revision"] == 1

        resp = c.post(
            f"/runs/{run_id}/metrics/refund_amount/revisions",
            json={
                "case_id": "c0",
                "score_revision": 2,
                "override_value": 0.0,
                "dispute_path": "judgment_wrong",
                "reason": "the refund was legitimate",
                "author_id": "reviewer-1",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["state"] == "applied"
        rev = body["revision"]
        assert rev["score_revision"] == 2
        assert rev["override_value"] == 0.0
        assert rev["dispute_path"] == "judgment_wrong"
        assert rev["reason"] == "the refund was legitimate"
        assert rev["machine_score_snapshot"] == {"status": "PASS", "score": 1.0}
        # The machine score is untouched, only flagged.
        assert body["machine_score"]["score_revision"] == 1
        assert body["machine_score"]["overridden"] is True

        # Diffable: the run view carries both the machine row and the ledger.
        got = c.get(f"/runs/{run_id}").json()
        assert got["cases"][0]["metrics"][0]["overridden"] is True
        assert got["cases"][0]["metrics"][0]["score"] == 1.0
        assert len(got["revisions"]) == 1

    def test_revision_idempotent(self, client):
        c, _, _ = client
        run_id, _ = run_to_complete(c)
        payload = {
            "case_id": "c0",
            "score_revision": 2,
            "override_value": 0.0,
            "reason": "duplicate submit",
        }
        assert c.post(f"/runs/{run_id}/metrics/refund_amount/revisions", json=payload).status_code == 201
        assert c.post(f"/runs/{run_id}/metrics/refund_amount/revisions", json=payload).status_code == 201
        got = c.get(f"/runs/{run_id}").json()
        assert len(got["revisions"]) == 1

    def test_revision_unknown_metric(self, client):
        c, _, _ = client
        created = create_run(c)
        run_id = created.json()["run"]["run_id"]
        resp = c.post(
            f"/runs/{run_id}/metrics/nope/revisions",
            json={"case_id": "c0", "score_revision": 1},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_revision_no_machine_row(self, client):
        c, _, _ = client
        created = create_run(c)
        run_id = created.json()["run"]["run_id"]
        resp = c.post(
            f"/runs/{run_id}/metrics/refund_amount/revisions",
            json={"case_id": "ghost", "score_revision": 1},
        )
        assert resp.status_code == 404
        assert "references the metric result it revises" in resp.json()["error"]["message"]


class TestPagination:
    def test_cursor_pagination(self, client):
        c, _, _ = client
        for _ in range(3):
            create_run(c)
        first = c.get("/runs", params={"limit": 2}).json()
        assert len(first["runs"]) == 2
        assert first["next_cursor"] is not None
        second = c.get("/runs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
        assert len(second["runs"]) == 1
        assert second["next_cursor"] is None
        ids1 = {r["run_id"] for r in first["runs"]}
        ids2 = {r["run_id"] for r in second["runs"]}
        assert not ids1 & ids2
        assert ids1 | ids2 == {r["run_id"] for r in c.get("/runs").json()["runs"]}

    def test_pagination_limit_bounds(self, client):
        c, _, _ = client
        assert c.get("/runs", params={"limit": 0}).status_code == 422
        assert c.get("/runs", params={"limit": 501}).status_code == 422


class TestRetrySurface:
    def test_retry_rows_surface_both_sides(self, client, monkeypatch):
        """Retry rows replace first-attempt rows in storage; the API must
        surface both: the metric row carries on_retry_override /
        is_authoritative flags, and the case's attempts list the first
        attempt and the retry side by side."""
        monkeypatch.setenv("FAKE_AGENT_OUTPUT_AMOUNT_ATTEMPT_0", "0.0")
        c, _, _ = client
        run_id, final = run_to_complete(c, retry_max=1)
        assert final["run"]["status"] == "complete"
        case = final["cases"][0]
        row = case["metrics"][0]
        assert row["status"] == "PASS"  # the retry won
        assert row["is_authoritative"] is False
        assert row["on_retry_override"] is True
        attempts = {a["attempt"]: a for a in case["attempts"]}
        assert 0 in attempts and 1 in attempts
        assert attempts[0]["is_first_attempt"] is True
        assert attempts[1]["is_first_attempt"] is False


class TestDashboardApi:
    """§37B server-side surface: the registry and the resolved default
    definition are served as data over /api/dashboards/* — the UI renders
    from this contract, never from backend-invented HTML."""

    def test_components_endpoint(self, client):
        c, _, _ = client
        resp = c.get("/api/dashboards/components")
        assert resp.status_code == 200
        body = resp.json()
        assert "registry_version" in body
        assert body["components"]
        names = {comp["name"] for comp in body["components"]}
        assert {"metric_summary", "run_table", "case_table"} <= names
        for comp in body["components"]:
            assert {"name", "version", "inputs"} <= comp.keys()
            for inp in comp["inputs"]:
                assert {"name", "type", "required"} <= inp.keys()

    def test_default_dashboard_placeholder_without_run(self, client):
        """No runs yet — the definition resolves to a placeholder, never a
        crash (§37B.3 empty-workspace behavior)."""
        c, _, _ = client
        resp = c.get("/api/dashboards/default")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run"] is None
        assert body["render_final"] is False
        assert body["blocks"]

    def test_default_dashboard_resolves_completed_run(self, client):
        c, _, _ = client
        run_id, _ = run_to_complete(c)
        resp = c.get("/api/dashboards/default", params={"run_id": run_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["run"]["run_id"] == run_id
        assert body["metrics_complete"] is True
        assert body["render_final"] is True
        by_component = {b["component"]: b for b in body["blocks"]}
        run_table = by_component["run_table"]["payload"]
        assert run_table["metrics"][0]["metric_id"] == "refund_amount"
        assert run_table["metrics"][0]["aggregation_state"] == "COMPLETE"
        case_table = by_component["case_table"]["payload"]
        assert len(case_table["cases"]) == 2
        row = case_table["cases"][0]["metrics"][0]
        assert row["metric_id"] == "refund_amount"
        assert row["status"] == "PASS"
        assert row["provisional"] is False
