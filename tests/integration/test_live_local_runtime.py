from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from llm_agent_eval.runtime.build import BuildService, RuntimeProfile
from llm_agent_eval.targets.local import LocalTargetAdapter


def test_live_rootless_local_target_round_trip():
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    image = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", "docker.io/library/alpine:3.20"],
        check=False, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not image:
        pytest.skip("the pinned Alpine base image is not available locally")
    source = Path(__file__).parents[1] / "fixtures" / "local-shell-agent"
    base = image[0]
    job = BuildService().prepare(
        source, RuntimeProfile("sha256:" + base, ("/bin/sh", "/source/agent.sh"))
    )
    result = LocalTargetAdapter().invoke(
        {"image": job.image_digest, "source_dir": str(source), "entrypoint": list(job.entrypoint)},
        {"question": "hello"},
        {"run_id": "live-run", "workspace_id": "live-ws", "case_id": "live-case", "attempt_id": "live-attempt"},
    )
    assert result.outcome == "ok"
    assert result.output["answer"] == "ok"
