"""R2 asynchronous remote-job adapter with explicit uncertainty."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import CancellationResult, InvocationResult


@dataclass(frozen=True)
class RemoteJob:
    remote_job_id: str
    state: str


class AsyncJobAdapter:
    def __init__(self, *, submit: Callable[[dict], dict], poll: Callable[[str], dict]):
        self._submit, self._poll = submit, poll

    def submit(self, payload: dict[str, Any]) -> RemoteJob:
        response = self._submit(payload)
        job_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("remote_job_id_missing")
        return RemoteJob(job_id, "submitted")

    def poll(self, remote_job_id: str) -> InvocationResult:
        response = self._poll(remote_job_id)
        state = response.get("state") if isinstance(response, dict) else None
        if state == "completed":
            return InvocationResult("ok", response.get("output"), {"final_output": "observed"})
        if state in {"failed", "cancelled"}:
            return InvocationResult(state, None, {"final_output": "unavailable"}, remote_uncertainty=state)
        return InvocationResult("pending", None, {"final_output": "unavailable"})

    def cancel(self, remote_job_id: str) -> CancellationResult:
        return CancellationResult("uncertain", remote_job_id, observed=False)
