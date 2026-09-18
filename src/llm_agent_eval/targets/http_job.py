"""R2 bounded asynchronous HTTP job target."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from .network_policy import EndpointPolicy, PolicyDenied
from .transport import PinnedTransport


@dataclass(frozen=True)
class RemoteJob:
    remote_job_id: str
    state: str


class AsyncJobAdapter:
    """Small callback contract retained for non-HTTP R2 integrations."""

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


class HttpJobAdapter:
    def __init__(self, *, policy: EndpointPolicy):
        self.policy = policy

    def verify(self, target_version, smoke_input, execution_context) -> VerificationRecord:
        result = self.invoke({"target": target_version}, smoke_input, execution_context)
        return VerificationRecord(
            state="verified" if result.outcome == "ok" else result.outcome,
            capabilities=result.capabilities, outcome=result.outcome,
            observed_identity=result.connector_observations or None,
            remote_uncertainty=result.remote_uncertainty,
        )

    def invoke(self, invocation_manifest, case_input, execution_context):
        target = invocation_manifest.get("target", invocation_manifest)
        caps = {"final_output": "unavailable", "tool_execution": "unavailable", "provider_cost": "unavailable"}
        try:
            submit_pin = self.policy.authorize(target["submit_url"])
            status_template = target["status_url_template"]
            self.policy.authorize(status_template.replace("{job_id}", "__job_id__"))
            timeout = float(target.get("poll_timeout_seconds", 30))
            interval = float(target.get("poll_interval_seconds", 0.1))
            if timeout <= 0 or interval < 0:
                raise ValueError("invalid polling limits")
        except (KeyError, TypeError, ValueError, PolicyDenied) as exc:
            return InvocationResult("policy_rejected", None, caps,
                                    connector_observations={"error": type(exc).__name__},
                                    remote_uncertainty="blocked")
        headers = {"Content-Type": "application/json"}
        if (target.get("auth") or {}).get("type") == "bearer":
            headers["Authorization"] = f"Bearer {execution_context.get('secret') or ''}"
        try:
            with httpx.Client(transport=PinnedTransport(submit_pin), follow_redirects=False,
                              timeout=timeout, verify=True, trust_env=False) as client:
                job_id = execution_context.get("remote_job_id")
                if not isinstance(job_id, str) or not job_id:
                    response = client.post(target["submit_url"], json=case_input, headers=headers)
                    if response.status_code < 200 or response.status_code >= 300:
                        return InvocationResult("protocol_error", None, caps)
                    payload = response.json()
                    job_id = payload.get("id") if isinstance(payload, dict) else None
                    if not isinstance(job_id, str) or not job_id:
                        return InvocationResult("protocol_error", None, caps)
                    persist = execution_context.get("persist_remote_job")
                    if callable(persist):
                        persist(job_id)
                observations = {"remote_job_id": job_id, "submitted": True}
                deadline = time.monotonic() + timeout
                while time.monotonic() <= deadline:
                    poll_url = status_template.replace("{job_id}", quote(job_id, safe=""))
                    poll_pin = self.policy.authorize(poll_url)
                    with httpx.Client(transport=PinnedTransport(poll_pin), follow_redirects=False,
                                      timeout=timeout, verify=True, trust_env=False) as poller:
                        status = poller.get(poll_url, headers=headers)
                    if status.status_code != 200:
                        return InvocationResult("protocol_error", None, caps,
                                                connector_observations=observations)
                    state = status.json()
                    state_name = state.get("state") if isinstance(state, dict) else None
                    if state_name == "completed":
                        return InvocationResult("ok", state.get("output"), {"final_output": "observed"},
                                                connector_observations={**observations, "completed": True})
                    if state_name in {"failed", "cancelled"}:
                        return InvocationResult(state_name, None, caps,
                                                connector_observations=observations,
                                                remote_uncertainty=state_name)
                    time.sleep(interval)
                return InvocationResult("timeout", None, caps,
                                        connector_observations=observations,
                                        remote_uncertainty="poll_timeout")
        except httpx.TimeoutException:
            return InvocationResult("timeout", None, caps, remote_uncertainty="timeout_after_possible_action")
        except (httpx.HTTPError, ValueError):
            return InvocationResult("transport_error", None, caps, remote_uncertainty="transport")

    def cancel(self, invocation_id, execution_context):
        return CancellationResult("uncertain", invocation_id, observed=False)
