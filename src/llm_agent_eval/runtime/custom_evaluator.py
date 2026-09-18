"""Separate bounded custom-evaluator execution contract."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .sandbox import PodmanSandbox, SandboxDenied, SandboxRequest, snapshot_digest


class EvaluatorDenied(ValueError):
    pass


class CustomEvaluatorSandbox:
    """Run reviewed evaluator code in a fresh network-disabled container."""

    def __init__(self, *, sandbox: PodmanSandbox | None = None):
        self.sandbox = sandbox or PodmanSandbox()

    def evaluate(self, *, image: str, source_dir: Path, entrypoint: tuple[str, ...],
                 evidence: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence, dict) or not isinstance(reference, dict):
            raise EvaluatorDenied("evaluator input must be JSON objects")
        if not source_dir.is_dir() or source_dir.is_symlink():
            raise EvaluatorDenied("evaluator source is unavailable")
        with tempfile.TemporaryDirectory(prefix="llm-agent-evaluator-") as temporary:
            inputs = Path(temporary) / "input"
            inputs.mkdir()
            (inputs / "evidence.json").write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
            (inputs / "reference.json").write_text(json.dumps(reference, sort_keys=True), encoding="utf-8")
            request = SandboxRequest(
                image=image, source_dir=source_dir, source_digest=snapshot_digest(source_dir),
                input_dir=inputs, input_digest=snapshot_digest(inputs), argv=entrypoint,
                egress="none",
            )
            result = self.sandbox.run(request)
        if result.state != "completed":
            raise EvaluatorDenied(result.code or "evaluator_runtime_failed")
        raw = result.output_files.get("score.json")
        if raw is None:
            raise EvaluatorDenied("evaluator_output_missing")
        try:
            output = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise EvaluatorDenied("evaluator_output_invalid") from exc
        if not isinstance(output, dict) or type(output.get("score")) not in {int, float}:
            raise EvaluatorDenied("evaluator_score_invalid")
        if not isinstance(output.get("evidence_refs", []), list):
            raise EvaluatorDenied("evaluator_evidence_refs_invalid")
        return output
