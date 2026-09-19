"""Local target adapter backed by the rootless per-case sandbox."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any
import uuid

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from ..events import ErrorBlock, RedactionState, Source, TraceEvent, make_event
from ..redaction import redact
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
            # The source snapshot is a worker-owned runtime detail. It is
            # supplied through ephemeral execution context, never frozen into
            # the durable run manifest.
            source = Path(execution_context.get("source_dir") or invocation_manifest["source_dir"])
            image = invocation_manifest["image"]
            entrypoint = tuple(invocation_manifest["entrypoint"])
            if not source.is_dir() or source.is_symlink():
                raise SandboxDenied("source snapshot is unavailable")
            source_digest = execution_context.get("source_snapshot_tree") or snapshot_digest(source)
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
                    proxy_endpoint=execution_context.get("proxy_endpoint"),
                    network_name=execution_context.get("network_name"),
                    network_run_id=execution_context.get("network_run_id"),
                    ca_cert=execution_context.get("ca_cert"),
                )
                cancel = execution_context.get("should_cancel")
                result = (
                    self.sandbox.run(request, should_cancel=cancel)
                    if callable(cancel) else self.sandbox.run(request)
                )
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
        trace_events: list[TraceEvent] = []
        raw_trace = result.output_files.get("trace.jsonl")
        if raw_trace is not None:
            try:
                for line in raw_trace.decode("utf-8").splitlines():
                    if line.strip():
                        trace_events.append(TraceEvent.model_validate_json(line))
            except (UnicodeDecodeError, ValueError):
                return InvocationResult(
                    outcome="invalid_trace", output=None,
                    capabilities={"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"},
                )
        trace_events = self._sanitize_agent_events(trace_events, execution_context)
        return InvocationResult(
            outcome="ok", output=output,
            capabilities={"final_output": "observed", "tool_execution": "declared", "provider_cost": "unavailable"},
            connector_observations={"container_name": result.container_name, "cleanup": result.cleanup},
            trace_events=tuple(trace_events),
        )

    @staticmethod
    def _sanitize_agent_events(
        events: list[TraceEvent], execution_context: dict[str, Any],
    ) -> list[TraceEvent]:
        """Rebind trace-file events at the trusted local-adapter boundary.

        The sandbox can write arbitrary JSONL.  Its events are useful adapter
        evidence, but they cannot claim proxy authority, provider identity,
        metered cost, run identity, or capture-time redaction.  Generate a
        fresh event envelope, redact payload/error content, and retain only
        causal parent links that point to another event in this same file.
        """
        run_id = str(execution_context.get("run_id", "local-run"))
        workspace_id = str(execution_context.get("workspace_id", "default"))
        case_id = str(execution_context.get("case_id", "local-case"))
        attempt_id = str(execution_context.get("attempt_id", "local-attempt"))
        repeat_index = int(execution_context.get("repeat_index", 0))
        attempt = int(execution_context.get("attempt", 0))
        trusted_ids: dict[str, str] = {}
        sanitized: list[TraceEvent] = []

        for sequence, event in enumerate(events):
            safe_payload = redact(event.payload) if event.payload is not None else None
            safe_error = redact(event.error.model_dump()) if event.error is not None else None
            flags = set()
            truncated = False
            if safe_payload is not None:
                flags.update(safe_payload.detector_flags)
                truncated = truncated or safe_payload.truncated
            if safe_error is not None:
                flags.update(safe_error.detector_flags)
                truncated = truncated or safe_error.truncated
            redaction_state = RedactionState(
                status="truncated" if truncated else ("redacted" if flags else "clean"),
                rules=sorted(flags),
            )
            event_id = uuid.uuid4().hex
            trusted_ids[event.event_id] = event_id
            parent_event_id = trusted_ids.get(event.parent_event_id)
            sanitized.append(make_event(
                event_type=event.type,
                event_id=event_id,
                run_id=run_id,
                workspace_id=workspace_id,
                case_id=case_id,
                attempt_id=attempt_id,
                repeat_index=repeat_index,
                attempt=attempt,
                sequence=sequence,
                source=Source.ADAPTER,
                timestamp=datetime.now(timezone.utc),
                duration_ms=event.duration_ms,
                parent_event_id=parent_event_id,
                tool=event.tool,
                otel=event.otel,
                redaction_state=redaction_state,
                payload=safe_payload.content if safe_payload is not None else None,
                error=ErrorBlock.model_validate(safe_error.content) if safe_error is not None else None,
            ))
        return sanitized

    def cancel(self, invocation_id, execution_context) -> CancellationResult:
        return CancellationResult(state="uncertain", invocation_id=invocation_id, observed=False)
