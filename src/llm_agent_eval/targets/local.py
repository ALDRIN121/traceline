"""Local target adapter backed by the rootless per-case sandbox."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from ..runtime.sandbox import (
    PodmanSandbox,
    SandboxDenied,
    SandboxLimits,
    SandboxRequest,
    snapshot_digest,
)


class LocalTargetAdapter:
    def __init__(self, *, sandbox: PodmanSandbox | None = None):
        self.sandbox = sandbox or PodmanSandbox()

    def verify(self, target_version, smoke_input, execution_context) -> VerificationRecord:
        result = self.invoke(target_version, smoke_input, execution_context)
        capabilities = {
            "final_output": "observed" if result.outcome == "ok" else "unavailable",
            "tool_execution": "declared",
            "provider_cost": "observed" if result.connector_observations.get("cost_observed") else "unavailable",
        }
        return VerificationRecord(
            state="verified" if result.outcome == "ok" else result.outcome,
            capabilities=capabilities, outcome=result.outcome,
            observed_identity=result.connector_observations or None,
            remote_uncertainty=result.remote_uncertainty,
        )

    def invoke(self, invocation_manifest, case_input, execution_context) -> InvocationResult:
        try:
            source = Path(invocation_manifest["source_dir"])
            image = invocation_manifest["image"]
            entrypoint = tuple(invocation_manifest["entrypoint"])
            if not source.is_dir() or source.is_symlink():
                raise SandboxDenied("source snapshot is unavailable")
            source_digest = snapshot_digest(source)
        except (KeyError, TypeError, ValueError, OSError, SandboxDenied):
            return InvocationResult(
                outcome="invalid_runtime_manifest", output=None,
                capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"},
            )

        with tempfile.TemporaryDirectory(prefix="llm-agent-local-case-") as work:
            input_dir = Path(work) / "input"
            input_dir.mkdir()
            (input_dir / "case.json").write_text(json.dumps(case_input, sort_keys=True), encoding="utf-8")
            (input_dir / "manifest.json").write_text(json.dumps({
                "version": 1,
                "run_id": execution_context.get("run_id", "local-run"),
                "case_id": execution_context.get("case_id", "local-case"),
                "attempt_id": execution_context.get("attempt_id", "local-attempt"),
                "repeat_index": execution_context.get("repeat_index", 0),
                "attempt": execution_context.get("attempt", 0),
                "timeout_seconds": invocation_manifest.get("timeout_seconds", 60),
            }, sort_keys=True), encoding="utf-8")
            input_digest = snapshot_digest(input_dir)
            try:
                limits = SandboxLimits(
                    timeout_seconds=float(invocation_manifest.get("timeout_seconds", 60)),
                    memory_mb=int(invocation_manifest.get("memory_mb", 512)),
                    cpus=float(invocation_manifest.get("cpus", 1.0)),
                    pids=int(invocation_manifest.get("pids", 64)),
                    output_mb=int(invocation_manifest.get("output_mb", 64)),
                )
                request = SandboxRequest(
                    image=image, source_dir=source, source_digest=source_digest,
                    input_dir=input_dir, input_digest=input_digest,
                    argv=entrypoint,
                    limits=limits,
                    egress=invocation_manifest.get("egress", "none"),
                )
                result = self.sandbox.run(request)
            except SandboxDenied as exc:
                return InvocationResult(
                    outcome="invalid_runtime_manifest", output=None,
                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"},
                    connector_observations={"error": str(exc)}, remote_uncertainty="blocked",
                )
        if result.state == "cancelled":
            return InvocationResult(outcome="cancelled", output=None,
                                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"},
                                    remote_uncertainty="cancelled")
        if result.state != "completed":
            timed_out = result.state == "timed_out" or result.code == "timed_out"
            return InvocationResult(
                outcome="timeout" if timed_out else "runtime_unavailable",
                output=None,
                capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"},
                connector_observations={"sandbox_code": result.code, "cleanup": result.cleanup},
                remote_uncertainty="blocked",
            )
        raw = result.output_files.get("result.json")
        if raw is None:
            return InvocationResult(outcome="no_output", output=None,
                                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"})
        try:
            output = json.loads(raw)
        except (TypeError, ValueError):
            return InvocationResult(outcome="invalid_output", output=None,
                                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"})
        if not isinstance(output, dict):
            return InvocationResult(outcome="invalid_output", output=None,
                                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"})
        return InvocationResult(
            outcome="ok", output=output,
            capabilities={"final_output": "observed", "tool_execution": "declared", "provider_cost": "unavailable"},
            connector_observations={"container_name": result.container_name, "cleanup": result.cleanup},
        )

    def cancel(self, invocation_id, execution_context) -> CancellationResult:
        return CancellationResult(state="uncertain", invocation_id=invocation_id, observed=False)
