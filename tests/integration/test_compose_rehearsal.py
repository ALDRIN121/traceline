"""Disposable Compose deployment rehearsal.

The test is opt-in because it builds images and owns a named disposable
Compose project. Release CI enables it explicitly; local default test runs do
not require Docker or a running daemon.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://127.0.0.1:8000"


def _compose(project: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"POSTGRES_MAINT_PASSWORD", "POSTGRES_APP_PASSWORD"}
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _request(path: str, *, token: str | None = None, method: str = "GET",
             body: bytes | None = None, content_type: str = "application/json",
             idempotency_key: str | None = None):
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return exc.code, payload


def _wait_for(path: str, *, token: str | None = None, timeout: float = 180):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            status, payload = _request(path, token=token)
            if status == 200:
                return payload
            last_error = f"HTTP {status}: {payload}"
        except (OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise AssertionError(f"Compose endpoint did not become ready: {path}: {last_error}")


@pytest.mark.integration
def test_compose_generated_credentials_and_worker_rehearsal():
    if os.environ.get("LLM_AGENT_EVAL_RUN_COMPOSE") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_COMPOSE=1 to run the disposable Compose rehearsal")
    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    version = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False,
    )
    if version.returncode:
        pytest.skip("Docker Compose is not available")

    project = f"eval-compose-{uuid.uuid4().hex[:12]}"
    # Always issue the project-scoped teardown after attempting startup. A
    # failed build or partial create can still leave containers/volumes.
    started = True
    try:
        up = _compose(project, "up", "-d", "--build")
        assert up.returncode == 0, "Compose failed to start the disposable deployment"

        _wait_for("/health")
        _wait_for("/readiness")

        token_result = _compose(
            project, "exec", "-T", "eval-engine", "sh", "-c",
            "cat /var/lib/llm-agent-eval/install-owner.token",
        )
        assert token_result.returncode == 0
        token = token_result.stdout.strip()
        assert token

        status, projects = _request("/api/projects", token=token)
        assert status == 200
        assert isinstance(projects.get("projects"), list)

        status, created = _request(
            "/api/projects", token=token, method="POST",
            body=json.dumps({"name": "compose-rehearsal", "entrypoint": ["python", "agent.py"]}).encode(),
        )
        assert status == 201
        project_id = created["project"]["project_id"]

        archive = io.BytesIO()
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("agent.py", "print('compose rehearsal')\n")
        status, uploaded = _request(
            "/api/uploads", token=token, method="POST", body=archive.getvalue(),
            content_type="application/zip",
        )
        assert status == 201
        status, queued = _request(
            f"/api/projects/{project_id}/imports", token=token, method="POST",
            body=json.dumps({"kind": "zip", "upload_id": uploaded["upload_id"]}).encode(),
            idempotency_key="compose-rehearsal-import",
        )
        assert status == 202, queued
        job_id = queued["job_id"]

        deadline = time.monotonic() + 120
        observed = None
        while time.monotonic() < deadline:
            status, observed = _request(f"/api/jobs/{job_id}", token=token)
            assert status == 200
            if observed["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2)
        assert observed is not None
        assert observed["status"] == "completed", observed
        assert observed["result"]["source_version_id"]
    finally:
        if started:
            down = _compose(project, "down", "-v", "--remove-orphans")
            assert down.returncode == 0, "Compose cleanup failed for the disposable deployment"
