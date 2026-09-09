"""Connection and hosted-target verification jobs."""

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
from .http_json import FORBIDDEN_HEADERS, HttpJsonAdapter, UNSUPPORTED_MODES, reject_golden_mapping
from .network_policy import EndpointPolicy
from .openapi import import_openapi


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
        if mode in UNSUPPORTED_MODES or mode != "stateless_json":
            raise WorkflowError("R1 supports only synchronous stateless JSON HTTP", code="unsupported_target_mode")
        if body.get("retry_max") not in (None, 0):
            raise WorkflowError("Remote retries are disabled by default", code="retries_forbidden")
        mapping = body.get("output_mapping") or {}
        reject_golden_mapping(mapping)
        if body.get("request_mapping"):
            reject_golden_mapping(body["request_mapping"])
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
            "kind": "http_json",
            "url": url,
            "method": body.get("method") or "POST",
            "auth": auth,
            "request_mapping": body.get("request_mapping") or {},
            "output_mapping": mapping or {"final_response": "/answer"},
            "timeout_seconds": body.get("timeout_seconds") or 5,
            "max_response_bytes": body.get("max_response_bytes") or 1_048_576,
            "max_concurrency": body.get("max_concurrency") or 2,
            "retry_max": 0,
            "mode": "stateless_json",
            "pinned": pin,
        }
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
        if auth.get("type") in {"bearer", "api_key"}:
            secrets = SecretStore(self.storage, actor, self.secret_key_path)
            context["secret"] = secrets.resolve(auth["secret_ref"])
        adapter = HttpJsonAdapter(EndpointPolicy(allow_exact=_allow_exact()))
        record = adapter.verify(version.content, smoke, context)
        expires = None
        if record.state == "verified":
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        return {
            "state": record.state,
            "capabilities": record.capabilities,
            "outcome": record.outcome,
            "observed_identity": record.observed_identity,
            "target_id": target_id,
            "target_version_id": version_id,
            "secret_ref": auth.get("secret_ref"),
            "expires_at": expires,
            "provider_cost": "unknown",
        }
