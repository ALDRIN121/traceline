"""Static deployment contracts; these do not certify a live Compose install."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_worker_is_continuous_and_shares_persistent_install_state():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    credential_init = services["credential-init"]
    api, worker = services["eval-engine"], services["eval-worker"]
    assert credential_init["image"] == "postgres:16-alpine"
    assert credential_init["restart"] == "no"
    assert "eval_credentials:/run/eval-credentials" in credential_init["volumes"]
    assert "POSTGRES_MAINT_PASSWORD" in credential_init["environment"]
    assert "POSTGRES_APP_PASSWORD" in credential_init["environment"]
    assert "POSTGRES_PASSWORD" not in credential_init["environment"]
    assert services["postgres"]["depends_on"]["credential-init"]["condition"] == "service_completed_successfully"
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/eval-credentials/maint_password"
    assert worker["build"] == api["build"]
    assert worker["command"] == ["eval-engine", "worker"]
    assert worker["restart"] == "unless-stopped"
    assert worker["depends_on"]["db-bootstrap"]["condition"] == "service_completed_successfully"
    for service in (api, worker):
        assert service["environment"]["LLM_AGENT_EVAL_ARTIFACT_ROOT"] == "/var/lib/llm-agent-eval"
        assert "eval_artifacts:/var/lib/llm-agent-eval" in service["volumes"]
        assert service["environment"]["DATABASE_URL"] == "postgresql://eval_app@postgres:5432/eval_db"
        assert service["environment"]["PGPASSFILE"] == "/run/eval-credentials/.pgpass"
        assert not service.get("privileged", False)
        assert all("sock" not in mount for mount in service["volumes"])
    assert "ports" not in worker
    assert worker["environment"]["DATABASE_URL"] == api["environment"]["DATABASE_URL"]
    assert "eval_artifacts" in compose["volumes"]
    assert "eval_credentials" in compose["volumes"]


def test_api_and_worker_have_explicit_readiness_healthchecks():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    api_check = services["eval-engine"].get("healthcheck")
    worker_check = services["eval-worker"].get("healthcheck")
    assert api_check["test"] == ["CMD", "curl", "--fail", "http://localhost:8000/readiness"]
    assert api_check["interval"] and api_check["timeout"] and api_check["retries"]
    assert worker_check["test"] == ["CMD-SHELL", "kill -0 1 && test -r /var/lib/llm-agent-eval"]


def test_release_ci_runs_tests_and_fail_closed_scans():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-checks.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert "compose-rehearsal" in jobs
    compose_job = jobs["compose-rehearsal"]
    compose_commands = "\n".join(
        step.get("run", "") for step in compose_job["steps"]
    )
    compose_env = "\n".join(
        str(step.get("env", {})) for step in compose_job["steps"]
    )
    assert "LLM_AGENT_EVAL_RUN_COMPOSE" in compose_env
    assert "test_compose_rehearsal.py" in compose_commands
    assert not compose_job.get("continue-on-error", False)
    platform_smoke = jobs["platform-smoke"]
    assert platform_smoke["strategy"]["matrix"]["os"] == [
        "ubuntu-latest", "macos-latest", "windows-latest",
    ]
    platform_commands = "\n".join(
        step.get("run", "") for step in platform_smoke["steps"]
    )
    assert "requirements-dev.lock" in platform_commands
    assert "not live and not integration" in platform_commands
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


def test_bootstrap_accepts_a_generated_application_password_file(tmp_path, monkeypatch):
    from llm_agent_eval import bootstrap_postgres

    password_file = tmp_path / "app_password"
    password_file.write_text("generated-secret", encoding="utf-8")
    observed = {}

    def fake_provision(admin_url, username, password):
        observed.update(admin_url=admin_url, username=username, password=password)

    monkeypatch.setattr(bootstrap_postgres, "provision_application_login", fake_provision)
    monkeypatch.setenv("DATABASE_URL", "postgresql://eval_maint@postgres:5432/eval_db")
    monkeypatch.setenv("LLM_AGENT_EVAL_APP_DB_USER", "eval_app")
    monkeypatch.delenv("LLM_AGENT_EVAL_APP_DB_PASSWORD", raising=False)
    monkeypatch.setenv("LLM_AGENT_EVAL_APP_DB_PASSWORD_FILE", str(password_file))

    bootstrap_postgres.main()

    assert observed == {
        "admin_url": "postgresql://eval_maint@postgres:5432/eval_db",
        "username": "eval_app",
        "password": "generated-secret",
    }
