"""Separate bounded custom-evaluator execution contract."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
from typing import Any

from ..redaction import redact
from .sandbox import PodmanSandbox, SandboxDenied, SandboxRequest, snapshot_digest


class EvaluatorDenied(ValueError):
    pass


class CustomEvaluatorSandbox:
    """Run reviewed evaluator code in a fresh network-disabled container."""

    max_input_bytes = 256 * 1024
    max_output_bytes = 64 * 1024
    max_evidence_refs = 256
    max_evidence_ref_bytes = 255

    def __init__(self, *, sandbox: PodmanSandbox | None = None):
        self.sandbox = sandbox or PodmanSandbox()

    def evaluate(self, *, image: str, source_dir: Path, entrypoint: tuple[str, ...],
                 evidence: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence, dict) or not isinstance(reference, dict):
            raise EvaluatorDenied("evaluator input must be JSON objects")
        if not source_dir.is_dir() or source_dir.is_symlink():
            raise EvaluatorDenied("evaluator source is unavailable")
        safe_evidence = self._bounded_input(evidence, "evidence")
        safe_reference = self._bounded_input(reference, "reference")
        with tempfile.TemporaryDirectory(prefix="llm-agent-evaluator-") as temporary:
            inputs = Path(temporary) / "input"
            inputs.mkdir()
            (inputs / "evidence.json").write_text(safe_evidence, encoding="utf-8")
            (inputs / "reference.json").write_text(safe_reference, encoding="utf-8")
            request = SandboxRequest(
                image=image, source_dir=source_dir, source_digest=self._source_digest(source_dir),
                input_dir=inputs, input_digest=snapshot_digest(inputs), argv=entrypoint,
                egress="none",
            )
            result = self.sandbox.run(request)
        if result.state != "completed" or result.cleanup != "complete":
            raise EvaluatorDenied(result.code or "evaluator_runtime_failed")
        raw = result.output_files.get("score.json")
        if raw is None:
            raise EvaluatorDenied("evaluator_output_missing")
        if not isinstance(raw, bytes) or len(raw) > self.max_output_bytes:
            raise EvaluatorDenied("evaluator_output_too_large")
        try:
            output = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise EvaluatorDenied("evaluator_output_invalid") from exc
        if not isinstance(output, dict) or type(output.get("score")) not in {int, float}:
            raise EvaluatorDenied("evaluator_score_invalid")
        score = output["score"]
        if not math.isfinite(float(score)) or abs(float(score)) > 1_000_000:
            raise EvaluatorDenied("evaluator_score_invalid")
        refs = output.get("evidence_refs", [])
        if (not isinstance(refs, list) or len(refs) > self.max_evidence_refs
                or any(type(ref) is not str or not ref or len(ref.encode("utf-8")) > self.max_evidence_ref_bytes
                       for ref in refs)
                or len(set(refs)) != len(refs)):
            raise EvaluatorDenied("evaluator_evidence_refs_invalid")
        if "justification" in output and (
                type(output["justification"]) is not str
                or len(output["justification"].encode("utf-8")) > self.max_evidence_ref_bytes * 32):
            raise EvaluatorDenied("evaluator_justification_invalid")
        if set(output) - {"score", "evidence_refs", "justification"}:
            raise EvaluatorDenied("evaluator_output_schema_invalid")
        return output

    def _bounded_input(self, value: dict[str, Any], name: str) -> str:
        try:
            original = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise EvaluatorDenied(f"{name}_invalid") from exc
        if len(original.encode("utf-8")) > self.max_input_bytes:
            raise EvaluatorDenied("input_too_large")
        safe = redact(value, max_string_bytes=16 * 1024).content
        return json.dumps(safe, sort_keys=True, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _source_digest(source_dir: Path) -> dict[str, Any]:
        try:
            return snapshot_digest(source_dir)
        except (OSError, SandboxDenied) as exc:
            raise EvaluatorDenied("evaluator_source_invalid") from exc
