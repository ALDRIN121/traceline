import json
import os
import uuid
import pytest
from llm_agent_eval.storage import Storage

PG_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/eval_db")

@pytest.fixture
def pg_storage():
    try:
        store = Storage(PG_URL)
        store.create_schema()
        return store
    except Exception as exc:
        pytest.skip(f"PostgreSQL not accessible at {PG_URL}: {exc}")

def test_postgres_project_and_smoke(pg_storage):
    ws = f"ws_{uuid.uuid4().hex[:8]}"
    project = pg_storage.create_project(
        workspace_id=ws,
        name="pg_test_project",
        entrypoint=["python", "fixtures/sample-agent"],
    )
    assert project.name == "pg_test_project"
    assert project.workspace_id == ws

    fetched = pg_storage.get_project(project.project_id, ws)
    assert fetched is not None
    assert fetched.project_id == project.project_id

def test_postgres_run_lifecycle(pg_storage):
    ws = f"ws_{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4().hex
    spec = {
        "spec_version": "0.1.0",
        "name": "pg-test-suite",
        "dataset_version": "v1",
        "cases": [{"case_id": "c1", "name": "Case 1", "input": {"q": "hi"}}],
        "metrics": [{
            "metric_id": "m1",
            "name": "Metric 1",
            "type": "scalar",
            "target": {"type": "model_output", "occurrence": "last", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "hi"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
        }]
    }

    run = pg_storage.create_run(
        workspace_id=ws,
        spec_json=json.dumps(spec),
        agent_version={"entrypoint": "python agent.py"},
        tier="quick",
        repeat_config={"repeats": 1, "retry": {"max": 0, "authoritative": "first"}},
        world_config={"snapshot": "test"},
        case_count=1,
        concurrency=1,
        budget_usd_micros=1000000,
    )
    run_id = run.run_id
    assert run.status == "draft"
    pg_storage.set_run_status(run_id, ws, "queued")
    fetched_run = pg_storage.get_run(run_id, ws)
    assert fetched_run is not None
    assert fetched_run.run_id == run_id
    assert fetched_run.status == "queued"

    from llm_agent_eval.spec import TestCase
    tc = TestCase(case_id="c1", name="Case 1", input={"q": "hi"})
    pg_storage.insert_cases(run_id, ws, [tc], repeat_count=1)
    case = pg_storage.get_case(run_id, "c1", ws)
    assert case is not None
    assert case.case_id == "c1"

    rev = pg_storage.append_score_revision(
        run_id=run_id,
        case_id="c1",
        metric_id="m1",
        workspace_id=ws,
        score_revision=2,
        machine_score_snapshot={"score": 0.0, "status": "FAIL"},
        override_value={"value": 1.0, "status": "PASS"},
        dispute_path="rule_wrong",
        reason="Manual audit override",
        author_id="reviewer@test",
        status="resolved",
    )
    assert rev.status == "resolved"

    revisions = pg_storage.get_score_revisions(run_id=run_id, workspace_id=ws)
    assert len(revisions) == 1
    assert revisions[0].status == "resolved"

def test_postgres_full_engine_run(pg_storage):
    import sys
    from llm_agent_eval.engine import Engine
    from llm_agent_eval.spec import validate_spec

    ws = f"ws_{uuid.uuid4().hex[:8]}"
    spec_dict = {
        "spec_version": "0.1.0",
        "name": "pg-engine-test-suite",
        "dataset_version": "v1",
        "cases": [{
            "case_id": "order_delivered",
            "name": "Delivered Order",
            "input": {"order_id": "12345", "customer_query": "where is my order?"}
        }],
        "metrics": [{
            "metric_id": "responds",
            "name": "agent responds",
            "type": "scalar",
            "target": {"type": "model_output", "occurrence": "last", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "hi"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"}
        }]
    }

    spec = validate_spec(spec_dict)
    engine = Engine(storage=pg_storage)
    run = engine.create_run(
        workspace_id=ws,
        spec=spec,
        tier="quick",
        entrypoint=[sys.executable, "fixtures/sample-agent"],
        cwd=".",
    )
    assert run.status == "draft"

    result = engine.run(run.run_id, ws)
    assert result.status == "complete"
    assert len(result.case_results) == 1
    assert result.case_results[0].status == "completed"

    stored_run = pg_storage.get_run(run.run_id, ws)
    assert stored_run is not None
    assert stored_run.status == "complete"
