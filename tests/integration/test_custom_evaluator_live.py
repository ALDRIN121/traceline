"""Opt-in live proof for the reviewed custom-evaluator sandbox."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from llm_agent_eval.runtime.custom_evaluator import CustomEvaluatorSandbox


@pytest.mark.live
def test_live_custom_evaluator_runs_in_fresh_rootless_podman(tmp_path: Path):
    """A real evaluator image must produce only the bounded score contract."""
    if os.environ.get("LLM_AGENT_EVAL_RUN_CUSTOM_EVALUATOR") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_CUSTOM_EVALUATOR=1 to run Podman proof")
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")

    image_ids = subprocess.run(
        ["podman", "images", "--format", "{{.Id}}", "docker.io/library/alpine:3.20"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    if not image_ids:
        pytest.skip("the pinned Alpine base image is not available locally")
    image = image_ids[0]
    if not image.startswith("sha256:"):
        image = "sha256:" + image

    source = tmp_path / "evaluator"
    source.mkdir()
    (source / "evaluate.sh").write_text(
        "#!/bin/sh\n"
        "test -s /input/evidence.json\n"
        "test -s /input/reference.json\n"
        "printf '%s' '{\"score\":0.75,\"evidence_refs\":[\"event-1\"]}' > /output/score.json\n",
        encoding="utf-8",
    )

    result = CustomEvaluatorSandbox().evaluate(
        image=image,
        source_dir=source,
        entrypoint=("/bin/sh", "/source/evaluate.sh"),
        evidence={"answer": "ok"},
        reference={"expected": "ok"},
    )

    assert result == {"score": 0.75, "evidence_refs": ["event-1"]}
