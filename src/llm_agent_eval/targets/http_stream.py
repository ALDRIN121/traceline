"""R2 synchronous streaming HTTP adapter."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from ..streaming import SSEParser, StreamProtocolError


class HttpStreamAdapter:
    def verify(self, target_version, smoke_input, execution_context) -> VerificationRecord:
        result = self.invoke(target_version, smoke_input, execution_context)
        return VerificationRecord(
            state="verified" if result.outcome == "ok" else result.outcome,
            capabilities=result.capabilities, outcome=result.outcome,
            observed_identity=result.connector_observations or None,
            remote_uncertainty=result.remote_uncertainty,
        )

    def invoke(self, invocation_manifest, case_input, execution_context) -> InvocationResult:
        target = invocation_manifest.get("target", invocation_manifest)
        if target.get("mode") not in {"stream", "streaming"}:
            return InvocationResult("unsupported_target_mode", None, {"final_output": "unavailable"})
        parser = SSEParser(
            max_frame_bytes=int(target.get("max_frame_bytes", 64 * 1024)),
            max_frames=int(target.get("max_frames", 10_000)),
        )
        started = time.time()
        first_token = None
        final = None
        try:
            with httpx.Client(timeout=float(target.get("timeout_seconds", 30)), trust_env=False) as client:
                with client.stream("POST", target["url"], json=case_input,
                                   headers={"Accept": "text/event-stream"}) as response:
                    if response.status_code != 200:
                        return InvocationResult("protocol_error", None, {"final_output": "unavailable"})
                    for chunk in response.iter_bytes():
                        for frame in parser.feed(chunk):
                            if first_token is None:
                                first_token = time.time()
                            if frame.event in {"final", "done"}:
                                try:
                                    final = json.loads(frame.data)
                                except ValueError as exc:
                                    raise StreamProtocolError("final frame is not JSON") from exc
            parser.finish()
        except (httpx.HTTPError, StreamProtocolError, ValueError) as exc:
            return InvocationResult("stream_protocol_error", None, {"final_output": "unavailable"},
                                    connector_observations={"error": type(exc).__name__},
                                    remote_uncertainty="stream_uncertain")
        if final is None:
            return InvocationResult("incomplete_stream", None, {"final_output": "unavailable"},
                                    remote_uncertainty="stream_incomplete")
        return InvocationResult(
            "ok", final, {"final_output": "observed"},
            connector_observations={"stream_start": started, "first_token": first_token,
                                    "completed": time.time()},
        )

    def cancel(self, invocation_id, execution_context) -> CancellationResult:
        return CancellationResult("uncertain", invocation_id, observed=False)
