"""R2 scripted stateful HTTP sessions with explicit lifecycle cleanup."""

from __future__ import annotations

import httpx
from urllib.parse import quote

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from .network_policy import EndpointPolicy, PolicyDenied
from .transport import PinnedTransport


class HttpSessionAdapter:
    def __init__(self, *, policy: EndpointPolicy):
        self.policy = policy

    def verify(self, target_version, smoke_input, execution_context):
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
        turns = case_input.get("turns") if isinstance(case_input, dict) else None
        if not isinstance(turns, list) or not turns:
            return InvocationResult("script_required", None, caps, remote_uncertainty="blocked")
        try:
            init_pin = self.policy.authorize(target["init_url"])
            turn_template = target["turn_url_template"]
            self.policy.authorize(turn_template.replace("{session_id}", "__session_id__"))
            close_template = target.get("close_url_template")
            if close_template:
                self.policy.authorize(close_template.replace("{session_id}", "__session_id__"))
        except (KeyError, TypeError, PolicyDenied) as exc:
            return InvocationResult("policy_rejected", None, caps,
                                    connector_observations={"error": type(exc).__name__},
                                    remote_uncertainty="blocked")
        session_id = None
        try:
            with httpx.Client(transport=PinnedTransport(init_pin), follow_redirects=False,
                              timeout=float(target.get("timeout_seconds", 30)), verify=True,
                              trust_env=False) as client:
                response = client.post(target["init_url"], json={"case": case_input})
                payload = response.json() if response.content else {}
                session_id = payload.get("session_id") or payload.get("id") if isinstance(payload, dict) else None
                if response.status_code < 200 or response.status_code >= 300 or not isinstance(session_id, str):
                    return InvocationResult("protocol_error", None, caps)
                output = None
                for index, turn in enumerate(turns):
                    url = turn_template.replace("{session_id}", quote(session_id, safe=""))
                    pin = self.policy.authorize(url)
                    with httpx.Client(transport=PinnedTransport(pin), follow_redirects=False,
                                      timeout=float(target.get("timeout_seconds", 30)), verify=True,
                                      trust_env=False) as turn_client:
                        turn_response = turn_client.post(url, json=turn)
                    if turn_response.status_code != 200:
                        return InvocationResult("protocol_error", output, caps,
                                                connector_observations={"session_id": session_id, "turns_completed": index},
                                                remote_uncertainty="session_uncertain")
                    body = turn_response.json()
                    output = body.get("output", body) if isinstance(body, dict) else body
                if close_template:
                    close_url = close_template.replace("{session_id}", quote(session_id, safe=""))
                    close_pin = self.policy.authorize(close_url)
                    with httpx.Client(transport=PinnedTransport(close_pin), follow_redirects=False,
                                      timeout=float(target.get("timeout_seconds", 30)), verify=True,
                                      trust_env=False) as close_client:
                        closed = close_client.post(close_url, json={})
                    if closed.status_code >= 300:
                        return InvocationResult("cleanup_failed", output, caps,
                                                connector_observations={"session_id": session_id},
                                                remote_uncertainty="cleanup_failed")
                return InvocationResult("ok", output, {"final_output": "observed"},
                                        connector_observations={"session_id": session_id, "turns_completed": len(turns)})
        except (httpx.HTTPError, ValueError, PolicyDenied):
            return InvocationResult("transport_error", None, caps,
                                    connector_observations={"session_id": session_id} if session_id else {},
                                    remote_uncertainty="session_uncertain")

    def cancel(self, invocation_id, execution_context):
        return CancellationResult("uncertain", invocation_id, observed=False)
