"""Connection and hosted/local target services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid
from pathlib import Path

from ..auth import Actor
from ..contracts import NotFound, WorkflowError
from ..secrets import SecretStore
from ..storage import Storage, _now
from ..versions import VersionStore
from .http_json import (
    FORBIDDEN_HEADERS, HttpJsonAdapter, UNSUPPORTED_MODES, reject_golden_mapping,
    validate_retrieval_mapping,
)
from .http_openai import OpenAICompatibleAdapter
from .http_stream import HttpStreamAdapter
from .http_job import HttpJobAdapter
from .http_session import HttpSessionAdapter
from .frameworks import adapter_capability
from .network_policy import EndpointPolicy
from .openapi import import_openapi
from .local import LocalTargetAdapter

__all__ = ["ConnectionService", "LocalTargetAdapter"]


def _allow_exact() -> set[str]:
    raw = os.environ.get("EVAL_ENGINE_ALLOW_ENDPOINTS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


class ConnectionService:
    def __init__(self, storage: Storage, secret_key_path: Path | None = None):
        self.storage = storage
        self.secret_key_path = Path(secret_key_path) if secret_key_path else Path("install-secret.key")

    def create(self, actor: Actor, project_id: str, body: dict) -> dict:
        actor.require(write=True)
        if self.storage.get_project(project_id, actor.workspace_id) is None:
            raise NotFound()
        mode = body.get("mode") or "stateless_json"
        r2_enabled = os.environ.get("EVAL_ENGINE_ENABLE_R2", "false").lower() == "true"
        if mode in UNSUPPORTED_MODES and not (r2_enabled and mode in {"streaming", "async", "session"}):
            raise WorkflowError("R1 supports only synchronous stateless JSON HTTP", code="unsupported_target_mode")
        if mode not in {"stateless_json", "streaming", "async", "session"}:
            raise WorkflowError("target mode is not supported", code="unsupported_target_mode")
        framework = str(body.get("framework") or "generic_http").strip().lower()
        try:
            capability = adapter_capability(framework, version=mode, require_supported=True)
        except ValueError as exc:
            raise WorkflowError("target framework is not supported", code="unsupported_framework") from exc
        if body.get("retry_max") not in (None, 0):
            raise WorkflowError("Remote retries are disabled by default", code="retries_forbidden")
        mapping = body.get("output_mapping") or {}
        reject_golden_mapping(mapping)
        if body.get("request_mapping"):
            reject_golden_mapping(body["request_mapping"])
        retrieval_mapping = body.get("retrieval_mapping")
        if retrieval_mapping is not None:
            validate_retrieval_mapping(retrieval_mapping)
        url = body.get("url")
        if not isinstance(url, str) or not url:
            raise WorkflowError("url is required")
        auth = dict(body.get("auth") or {"type": "none"})
        auth_type = auth.get("type") or "none"
        if auth_type not in {"none", "bearer", "api_key"}:
            raise WorkflowError("R1 supports none, bearer, or API-key authentication", code="unsupported_auth")
        for field in ("token", "password", "api_key", "value", "secret"):
            if auth.get(field):
                raise WorkflowError("Inline credentials are not stored; use secret_ref", code="secret_ref_required")
        if auth_type in {"bearer", "api_key"}:
            if not auth.get("secret_ref"):
                raise WorkflowError("Bearer and API-key auth require an encrypted secret_ref",
                                    code="secret_ref_required")
            header = (auth.get("header") or "X-API-Key")
            if str(header).lower() in FORBIDDEN_HEADERS:
                raise WorkflowError("That request header cannot be overridden", code="forbidden_header")
        pin = EndpointPolicy(allow_exact=_allow_exact()).authorize(url)
        target_id, now = uuid.uuid4().hex, _now()
        content = {
            "kind": {"stateless_json": "http_json", "streaming": "http_stream", "async": "http_job", "session": "http_session"}[mode],
            "framework": capability.framework,
            "url": url,
            "method": body.get("method") or "POST",
            "auth": auth,
            "request_mapping": body.get("request_mapping") or {},
            "output_mapping": mapping or {"final_response": "/answer"},
            "retrieval_mapping": retrieval_mapping or None,
            "timeout_seconds": body.get("timeout_seconds") or 5,
            "max_response_bytes": body.get("max_response_bytes") or 1_048_576,
            "max_concurrency": body.get("max_concurrency") or 2,
            "retry_max": 0,
            "mode": "stateless_json" if mode == "stateless_json" else mode,
            "pinned": pin,
        }
        if mode == "streaming":
            content["mode"] = "stream"
            content["max_frame_bytes"] = body.get("max_frame_bytes") or 65_536
            content["max_frames"] = body.get("max_frames") or 10_000
        elif mode == "async":
            content["submit_url"] = body.get("submit_url") or url
            content["status_url_template"] = body.get("status_url_template")
            content["poll_interval_seconds"] = body.get("poll_interval_seconds") or 0.1
            content["poll_timeout_seconds"] = body.get("poll_timeout_seconds") or 30
            if not isinstance(content["status_url_template"], str) or "{job_id}" not in content["status_url_template"]:
                raise WorkflowError("async targets require status_url_template", code="target_invalid")
        elif mode == "session":
            content["init_url"] = body.get("init_url") or url
            content["turn_url_template"] = body.get("turn_url_template")
            content["close_url_template"] = body.get("close_url_template")
            if not isinstance(content["turn_url_template"], str) or "{session_id}" not in content["turn_url_template"]:
                raise WorkflowError("session targets require turn_url_template", code="target_invalid")
        with self.storage.workspace_transaction(actor.workspace_id) as conn:
            conn.execute(
                "INSERT INTO targets (workspace_id,target_id,project_id,created_at) VALUES (?,?,?,?)",
                (actor.workspace_id, target_id, project_id, now),
            )
        version = VersionStore(self.storage).create("target", target_id, content, 0, actor)
        return {"state": "configured", "target_id": target_id, "version_id": version.version_id,
                "revision": version.revision}

    def import_openapi(self, actor: Actor, project_id: str, body: dict) -> dict:
        actor.require(write=True)
        if self.storage.get_project(project_id, actor.workspace_id) is None:
            raise NotFound()
        document = body.get("document")
        if not isinstance(document, dict):
            raise WorkflowError("document is required", code="openapi_invalid")
        assistance = import_openapi(
            document,
            operation_id=body.get("operation_id"),
            path=body.get("path"),
            method=body.get("method"),
        )
        return {"state": "mapped", **assistance, "project_id": project_id}

    def verify(self, actor: Actor, target_id: str, body: dict) -> dict:
        actor.require(write=True)
        listing = VersionStore(self.storage).list("target", target_id, actor)
        version_id = body.get("target_version_id") or listing["active_version_id"]
        if not version_id:
            raise NotFound()
        version = VersionStore(self.storage).get(version_id, actor)
        smoke = body.get("smoke_input")
        if not isinstance(smoke, dict):
            raise WorkflowError("smoke_input is required")
        auth = version.content.get("auth") or {"type": "none"}
        context = {"workspace_id": actor.workspace_id, "job_id": body.get("job_id")}
        # Stateful targets need a bounded verification script just like a run
        # case. Keep it in the ephemeral verification context; it is not part
        # of the target version or a stored credential-bearing manifest.
        if isinstance(smoke.get("interaction_script"), list):
            context["interaction_script"] = smoke["interaction_script"]
        if auth.get("type") in {"bearer", "api_key"}:
            secrets = SecretStore(self.storage, actor, self.secret_key_path)
            context["secret"] = secrets.resolve(auth["secret_ref"])
        adapter = self._adapter(version.content)
        record = adapter.verify(version.content, smoke, context)
        expires = None
        verified_version_id = version_id
        if record.state == "verified":
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            # Verification is part of the immutable target identity used by a
            # later run plan. Keep the original configuration version intact;
            # publish a new target version containing only the observed receipt.
            verified_content = dict(version.content)
            verified_content["verification"] = {
                "state": "verified",
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires,
                "capabilities": dict(record.capabilities),
                "observed_identity": record.observed_identity,
                "outcome": record.outcome,
            }
            verified_version_id = VersionStore(self.storage).create(
                "target", target_id, verified_content, version.revision, actor
            ).version_id
        return {
            "state": record.state,
            "capabilities": record.capabilities,
            "outcome": record.outcome,
            "observed_identity": record.observed_identity,
            "target_id": target_id,
            "target_version_id": verified_version_id,
            "configured_target_version_id": version_id,
            "secret_ref": auth.get("secret_ref"),
            "expires_at": expires,
            "provider_cost": "unknown",
        }

    @staticmethod
    def _adapter(content: dict):
        policy = EndpointPolicy(allow_exact=_allow_exact())
        if content.get("kind") == "http_stream":
            return HttpStreamAdapter(policy=policy)
        if content.get("kind") == "http_job":
            return HttpJobAdapter(policy=policy)
        if content.get("kind") == "http_session":
            return HttpSessionAdapter(policy=policy)
        if content.get("framework") == "openai_compatible":
            return OpenAICompatibleAdapter(policy)
        return HttpJsonAdapter(policy)
