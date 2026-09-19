"""Static deployment contracts; these do not certify a live Compose install."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_worker_is_continuous_and_shares_persistent_install_state():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    api, worker = services["eval-engine"], services["eval-worker"]
    assert worker["build"] == api["build"]
    assert worker["command"] == ["eval-engine", "worker"]
    assert worker["restart"] == "unless-stopped"
    assert worker["depends_on"]["db-bootstrap"]["condition"] == "service_completed_successfully"
    for service in (api, worker):
        assert service["environment"]["LLM_AGENT_EVAL_ARTIFACT_ROOT"] == "/var/lib/llm-agent-eval"
        assert "eval_artifacts:/var/lib/llm-agent-eval" in service["volumes"]
        assert "eval_app:" in service["environment"]["DATABASE_URL"]
        assert not service.get("privileged", False)
        assert all("sock" not in mount for mount in service["volumes"])
    assert "ports" not in worker
    assert worker["environment"]["DATABASE_URL"] == api["environment"]["DATABASE_URL"]
    assert "eval_artifacts" in compose["volumes"]


def test_release_ci_runs_tests_and_fail_closed_scans():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-checks.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    tests = jobs["tests"]
    assert tests["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert "TEST_DATABASE_URL" in tests["env"]
    commands = "\n".join(step.get("run", "") for step in tests["steps"])
    assert "pytest" in commands
    assert "requirements-dev.lock" in commands
    assert "--no-deps" in commands
    audit = "\n".join(step.get("run", "") for step in jobs["dependency-audit"]["steps"])
    assert "pip-audit" in audit and "requirements.lock" in audit
    secrets = "\n".join(step.get("run", "") for step in jobs["secret-scan"]["steps"])
    assert "gitleaks" in secrets and "--redact" in secrets
    for job in jobs.values():
        assert not job.get("continue-on-error", False)
        for step in job["steps"]:
            assert not step.get("continue-on-error", False)
            assert "|| true" not in step.get("run", "")


def test_deployment_image_contains_postgres_client_for_operator_restore():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "postgres:16-bookworm AS postgres16-client" in dockerfile
    assert "COPY --from=postgres16-client" in dockerfile
    assert "pg_restore --version" in dockerfile
