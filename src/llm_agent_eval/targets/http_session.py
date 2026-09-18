"""R2 scripted stateful HTTP sessions with explicit lifecycle cleanup."""

from __future__ import annotations

import httpx
from urllib.parse import quote

from ..contracts import CancellationResult, InvocationResult, VerificationRecord
from ..events import EventType
from .network_policy import EndpointPolicy, PolicyDenied
from .evidence import adapter_event
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
        interaction_script = execution_context.get("interaction_script") or None
        if interaction_script is not None and (not isinstance(interaction_script, list) or not interaction_script):
            return InvocationResult("script_required", None, caps, remote_uncertainty="blocked")
        if interaction_script is None and (not isinstance(turns, list) or not turns):
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
        trace_events = []
        close_sent = False
        should_cancel = execution_context.get("should_cancel")

        def close_session() -> bool:
            nonlocal close_sent
            if close_sent or not session_id or not close_template:
                return True
            try:
                close_url = close_template.replace("{session_id}", quote(session_id, safe=""))
                close_pin = self.policy.authorize(close_url)
                with httpx.Client(transport=PinnedTransport(close_pin), follow_redirects=False,
                                  timeout=float(target.get("timeout_seconds", 30)), verify=True,
                                  trust_env=False) as close_client:
                    closed = close_client.post(close_url, json={})
                close_sent = True
                return closed.status_code < 300
            except (httpx.HTTPError, PolicyDenied, ValueError):
                return False

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
                turns_completed = 0
                if callable(should_cancel) and should_cancel():
                    return InvocationResult(
                        "cancelled", None, caps,
                        connector_observations={"session_id": session_id, "turns_completed": 0},
                        remote_uncertainty="cancelled",
                    )

                def send_turn(turn: dict[str, object]):
                    nonlocal turns_completed
                    url = turn_template.replace("{session_id}", quote(session_id, safe=""))
                    pin = self.policy.authorize(url)
                    with httpx.Client(transport=PinnedTransport(pin), follow_redirects=False,
                                      timeout=float(target.get("timeout_seconds", 30)), verify=True,
                                      trust_env=False) as turn_client:
                        turn_response = turn_client.post(url, json=turn)
                    if turn_response.status_code != 200:
                        return None, InvocationResult(
                            "protocol_error", output, caps,
                            connector_observations={"session_id": session_id, "turns_completed": turns_completed},
                            remote_uncertainty="session_uncertain",
                        )
                    body = turn_response.json()
                    turns_completed += 1
                    return body, None

                if interaction_script is not None:
                    observed = payload
                    pending_input = False
                    for step in interaction_script:
                        if callable(should_cancel) and should_cancel():
                            return InvocationResult(
                                "cancelled", output, caps,
                                connector_observations={
                                    "session_id": session_id,
                                    "turns_completed": turns_completed,
                                },
                                remote_uncertainty="cancelled",
                            )
                        if not isinstance(step, dict) or step.get("kind") not in {
                            "wait_for_input", "user_response"
                        }:
                            return InvocationResult("script_invalid", None, caps,
                                                    remote_uncertainty="blocked")
                        if step["kind"] == "wait_for_input":
                            requested = step.get("requested_input")
                            observed_prompt = observed.get("requested_input") if isinstance(observed, dict) else None
                            waiting = isinstance(observed, dict) and (
                                observed.get("state") == "awaiting_input" or observed_prompt is not None
                            )
                            if pending_input or not waiting or (
                                requested is not None and observed_prompt != requested
                            ):
                                return InvocationResult("approval_not_requested", None, caps,
                                                        remote_uncertainty="blocked")
                            event = adapter_event(
                                execution_context, EventType.WAIT_FOR_INPUT, len(trace_events),
                                {"requested_input": requested},
                            )
                            if event is not None:
                                trace_events.append(event)
                            pending_input = True
                            continue
                        provided = step.get("provided_input")
                        if not pending_input or not isinstance(provided, str):
                            return InvocationResult("script_invalid", None, caps,
                                                    remote_uncertainty="blocked")
                        event = adapter_event(
                            execution_context, EventType.USER_RESPONSE, len(trace_events),
                            {"provided_input": provided, "source": step.get("source", "simulator")},
                        )
                        if event is not None:
                            trace_events.append(event)
                        body, failure = send_turn({"input": provided})
                        if failure is not None:
                            return failure
                        output = body.get("output", body) if isinstance(body, dict) else body
                        observed = body
                        pending_input = False
                    if pending_input:
                        return InvocationResult("script_invalid", None, caps,
                                                remote_uncertainty="blocked")
                else:
                    for turn in turns or []:
                        if callable(should_cancel) and should_cancel():
                            return InvocationResult(
                                "cancelled", output, caps,
                                connector_observations={
                                    "session_id": session_id,
                                    "turns_completed": turns_completed,
                                },
                                remote_uncertainty="cancelled",
                            )
                        body, failure = send_turn(turn)
                        if failure is not None:
                            return failure
                        output = body.get("output", body) if isinstance(body, dict) else body
                if close_template:
                    if not close_session():
                        return InvocationResult("cleanup_failed", output, caps,
                                                connector_observations={"session_id": session_id},
                                                remote_uncertainty="cleanup_failed")
                return InvocationResult("ok", output, {"final_output": "observed"},
                                        connector_observations={"session_id": session_id, "turns_completed": turns_completed},
                                        trace_events=tuple(trace_events))
        except (httpx.HTTPError, ValueError, PolicyDenied):
            return InvocationResult("transport_error", None, caps,
                                    connector_observations={"session_id": session_id} if session_id else {},
                                    remote_uncertainty="session_uncertain")
        finally:
            # A script mismatch, transport failure, or cancellation after
            # initialization must still release the remote session.
            close_session()

    def cancel(self, invocation_id, execution_context):
        return CancellationResult("uncertain", invocation_id, observed=False)
