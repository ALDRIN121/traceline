from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from llm_agent_eval.api import create_app
from llm_agent_eval.orchestrator import OrchestratorAgent, ValidatorAgent
from llm_agent_eval.storage import Storage


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_TOOLS = {
    "lookup_order_status",
    "check_refund_eligibility",
    "search_articles",
}
SAMPLE_MODELS = {"deepseek-v4-flash", "gpt-4o"}

@pytest.fixture
def client():
    db = Storage(':memory:')
    db.create_schema()
    app = create_app(storage=db)
    return TestClient(app)

def test_validate_evaluation_spec(client):
    valid_spec = {
        'spec_version': '0.1.0',
        'name': 'test-suite',
        'dataset_version': 'v1',
        'cases': [{'case_id': 'c1', 'name': 'Case 1', 'input': {'q': 'hello'}}],
        'metrics': [{
            'metric_id': 'm1',
            'name': 'Metric 1',
            'type': 'scalar',
            'target': {'type': 'model_output', 'occurrence': 'last', 'on_missing': 'fail'},
            'evaluator': {'type': 'exact_match', 'expected': 'hello'},
            'scoring': {'type': 'binary', 'range': [0, 1]},
            'aggregation': {'method': 'pass_rate', 'on_error': 'fail'}
        }]
    }
    r = client.post('/api/evaluations/validate', json={'spec': valid_spec})
    assert r.status_code == 200
    assert r.json()['valid'] is True
    assert r.json()['validation_status'] == 'validated'
    assert r.json()['verification_id'] is None
    assert r.json()['verification_status'] == 'not_verified'
    assert 'smoke_status' not in r.json()
    assert 'smoke_passed' not in r.json()

    invalid_spec = {'spec_version': 'invalid'}
    r2 = client.post('/api/evaluations/validate', json={'spec': invalid_spec})
    assert r2.status_code == 200
    assert r2.json()['valid'] is False
    assert r2.json()['validation_status'] == 'failed'
    assert r2.json()['verification_id'] is None
    assert r2.json()['verification_status'] == 'not_verified'
    assert len(r2.json()['errors']) > 0


def test_validator_distinguishes_schema_validation_from_runtime_smoke():
    invalid = ValidatorAgent().validate_suite({'spec_version': 'invalid'})

    assert invalid['validation_status'] == 'failed'
    assert invalid['verification_id'] is None
    assert invalid['verification_status'] == 'not_verified'
    assert 'smoke_status' not in invalid


def test_unknown_source_never_becomes_sample(client):
    response = client.post(
        "/api/projects/analyze",
        json={"source": "/missing/traceline-agent"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_not_found"


def test_analysis_requires_an_explicit_source(client):
    response = client.post("/api/projects/analyze", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_required"


def test_unrelated_python_project_has_only_observed_findings(client, tmp_path):
    project = tmp_path / "unrelated-agent"
    project.mkdir()
    (project / "main.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")

    response = client.post("/api/projects/analyze", json={"source": str(project)})

    assert response.status_code == 200
    data = response.json()
    assert data["source_id"]
    assert data["source_path"] == str(project.resolve())
    assert data["verification_id"] is None
    assert data["verification_status"] == "not_verified"
    assert data["tools_detected"] == []
    assert data["models_detected"] == []
    assert data["sample_data"] == []
    assert "smoke_passed" not in data
    assert data["findings"]
    assert all(finding["status"] == "observed_static" for finding in data["findings"])
    assert all(finding["evidence"]["path"] for finding in data["findings"])


def test_broken_reference_fixture_gets_no_sample_capabilities_or_success(client):
    source = REPO_ROOT / "fixtures" / "reference-agent"

    response = client.post("/api/projects/analyze", json={"source": str(source)})

    assert response.status_code == 200
    data = response.json()
    assert not SAMPLE_TOOLS.intersection(data["tools_detected"])
    assert not SAMPLE_MODELS.intersection(data["models_detected"])
    assert data["sample_data"] == []
    assert data["verification_id"] is None
    assert data["verification_status"] == "not_verified"
    assert "smoke_passed" not in data


def test_explicit_sample_source_analysis_remains_available(client):
    response = client.post(
        "/api/projects/analyze",
        json={"source": "fixtures/sample-agent"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "sample-agent"
    assert data["source_id"]
    assert {"search_articles", "check_refund_eligibility"}.issubset(data["tools_detected"])
    assert data["verification_id"] is None
    assert data["verification_status"] == "not_verified"


def test_unknown_chat_source_is_not_replaced_with_sample(client):
    response = client.post(
        "/api/harness/chat",
        json={"message": "Analyze /missing/traceline-agent"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_not_found"


def test_chat_returns_observed_summaries_not_simulated_thoughts(client, tmp_path):
    project = tmp_path / "chat-agent"
    project.mkdir()
    (project / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")

    response = client.post(
        "/api/harness/chat",
        json={"message": f"Analyze {project}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["data"]["verification_id"] is None
    assert data["data"]["verification_status"] == "not_verified"
    assert data["data"]["tools_detected"] == []
    assert data["data"]["models_detected"] == []
    assert data["data"]["sample_data"] == []
    assert data["spec_data"] is None
    assert data["dashboard"] is None
    assert data["agent_steps"]
    assert all("thought" not in step for step in data["agent_steps"])
    assert all(step["summary"] for step in data["agent_steps"])


def test_failed_validation_does_not_create_successful_orchestration_stage(tmp_path):
    project = REPO_ROOT / "fixtures" / "sample-agent"
    orchestrator = OrchestratorAgent(workspace_root=REPO_ROOT)
    orchestrator.eval_generator.generate_suite = lambda *_args: {"spec_version": "invalid"}

    result = orchestrator.process_turn(f"Analyze {project}")

    assert result.spec_data is None
    assert result.dashboard is None
    assert result.project_data["validation_status"] == "failed"
    validator_step = next(
        step for step in result.agent_steps if step.agent_role == "validator"
    )
    assert validator_step.status == "failed"
    assert all(step.agent_role != "dashboard_creator" for step in result.agent_steps)

def test_sample_run_creation(client):
    r = client.post('/api/runs/sample')
    assert r.status_code == 202
    data = r.json()
    assert 'run_id' in data
    assert data['status'] == 'queued'

    r2 = client.get(f'/runs/{data["run_id"]}')
    assert r2.status_code == 200


def test_chat_persists_custom_eval_with_dashboard_and_pipeline(client):
    r = client.post("/api/harness/chat", json={"message": "Analyze fixtures/sample-agent"})
    assert r.status_code == 200
    data = r.json()
    assert data["eval_id"]
    assert data["spec_data"]["cases"]
    assert data["dashboard"]["blocks"]
    assert data["hitl"]["entrypoint"]
    stages = {s["id"]: s["status"] for s in data["pipeline"]}
    assert stages["connect"] == "complete"
    assert stages["hitl"] == "complete"
    assert stages["plan"] == "complete"
    assert stages["dataset"] == "complete"
    assert stages["dashboard"] == "complete"
    assert stages["run"] == "pending"
    assert stages["score"] == "pending"

    listed = client.get("/api/evals")
    assert listed.status_code == 200
    ids = [e["eval_id"] for e in listed.json()["evals"]]
    assert data["eval_id"] in ids

    detail = client.get(f"/api/evals/{data['eval_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["spec"]["name"] == data["spec_data"]["name"]
    assert len(body["dataset"]) == len(data["spec_data"]["cases"])


def test_custom_eval_run_uses_authored_spec(client):
    chat = client.post(
        "/api/harness/chat",
        json={"message": "Analyze fixtures/sample-agent and assert refund eligibility"},
    ).json()
    eval_id = chat["eval_id"]
    spec_name = chat["spec_data"]["name"]

    launched = client.post(f"/api/evals/{eval_id}/run")
    assert launched.status_code == 202
    run_id = launched.json()["run_id"]
    assert run_id

    run = client.get(f"/runs/{run_id}").json()
    assert run["run"]["spec"]["name"] == spec_name
    assert run["run"]["run_id"] == run_id

    detail = client.get(f"/api/evals/{eval_id}").json()
    assert detail["run_id"] == run_id
    stages = {s["id"]: s["status"] for s in detail["pipeline"]}
    assert stages["run"] in ("complete", "running", "queued", "complete")


def test_upload_zip_then_chat_binds_source(client, tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("agent.py", "def main():\n    return 0\n")
        zf.writestr("__main__.py", "print('ok')\n")
    buf.seek(0)

    up = client.post(
        "/api/projects/upload",
        files={"file": ("cs-agent.zip", buf, "application/zip")},
    )
    assert up.status_code == 200
    source = up.json()["source"]
    assert "cs-agent" in source or up.json()["name"]

    chat = client.post(
        "/api/harness/chat",
        json={"message": f"Analyze {up.json()['source']}"},
    )
    assert chat.status_code == 200
    assert chat.json()["data"]["source_id"]
    assert chat.json()["data"]["source_path"] == up.json()["source"]
    assert chat.json()["eval_id"] is None
    assert chat.json()["spec_data"] is None


def test_static_model_discovery_excludes_credentials_and_unrelated_names(client, tmp_path):
    project = tmp_path / "model-safety"
    project.mkdir()
    (project / "main.py").write_text(
        'MODEL_API_KEY = "synthetic-secret-for-review"\n'
        'car_model = "hatchback"\n'
        'model_name = "provider/real-model"\n',
        encoding="utf-8",
    )

    response = client.post("/api/projects/analyze", json={"source": str(project)})

    assert response.status_code == 200
    data = response.json()
    assert data["models_detected"] == ["provider/real-model"]
    assert "synthetic-secret-for-review" not in str(data)
    assert "hatchback" not in str(data)


def test_analyze_command_precedes_help_matching(client, tmp_path):
    missing = client.post(
        "/api/harness/chat",
        json={"message": "Analyze /missing/help-agent"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "source_not_found"

    project = tmp_path / "help-agent"
    project.mkdir()
    (project / "main.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    found = client.post("/api/harness/chat", json={"message": f"Analyze {project}"})
    assert found.status_code == 200
    assert found.json()["action"] == "analysis"
    assert found.json()["data"]["source_path"] == str(project.resolve())


def test_chat_and_direct_analysis_agree_on_entrypoint_command(client):
    direct = client.post(
        "/api/projects/analyze",
        json={"source": "fixtures/sample-agent"},
    )
    chat = client.post(
        "/api/harness/chat",
        json={"message": "Analyze fixtures/sample-agent"},
    )

    assert direct.status_code == 200
    assert chat.status_code == 200
    assert chat.json()["data"]["entrypoint_command"] == direct.json()["entrypoint_command"]
