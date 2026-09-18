"""Immutable run plans and explicit execution authorization.

Planning is deliberately separate from execution. A plan freezes the version
identities and limits that a later worker must use; authorization binds a
human/service decision to that exact content digest. No plan API executes an
agent or reports a measured result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping
import uuid

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .storage import RunAuthorizationRecord, RunPlanRecord, Storage, _now
from .versions import VersionStore

_VERSION_REF_KEYS = frozenset({
    "project_id", "source_version_id", "knowledge_version_id",
    "evaluation_version_id", "dataset_version_id", "target_version_id",
    "connection_version_id", "dashboard_version_id", "world_version_id",
    "model_selection_version_id",
})
_REQUIRED_VERSION_KEYS = ("evaluation_version_id", "dataset_version_id")
_TIERS = frozenset({"quick", "standard", "full"})


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowError("Run plan content must be finite JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunPlan:
    plan_id: str
    workspace_id: str
    content_digest: str
    state: str
    content: dict[str, Any]
    blockers: tuple[str, ...]
    actor_id: str
    created_at: str


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    workspace_id: str
    plan_id: str
    plan_hash: str
    actor_id: str
    state: str
    expires_at: str
    created_at: str


def _plan(record: RunPlanRecord) -> RunPlan:
    return RunPlan(
        plan_id=record.plan_id, workspace_id=record.workspace_id,
        content_digest=record.content_digest, state=record.state,
        content=record.content, blockers=tuple(record.blockers),
        actor_id=record.actor_id, created_at=record.created_at,
    )


def _authorization(record: RunAuthorizationRecord) -> Authorization:
    return Authorization(
        authorization_id=record.authorization_id, workspace_id=record.workspace_id,
        plan_id=record.plan_id, plan_hash=record.plan_hash,
        actor_id=record.actor_id, state=record.state,
        expires_at=record.expires_at, created_at=record.created_at,
    )


class RunPlanService:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.versions = VersionStore(storage)

    def plan_run(self, actor: Actor, version_refs: Mapping[str, Any], limits: Mapping[str, Any]) -> RunPlan:
        actor.require(write=True)
        if not isinstance(version_refs, Mapping):
            raise WorkflowError("version_refs must be an object")
        if not isinstance(limits, Mapping):
            raise WorkflowError("limits must be an object")
        refs = dict(version_refs)
        unknown = sorted(set(refs) - _VERSION_REF_KEYS)
        if unknown:
            raise WorkflowError("Unknown run-plan version reference", details={"keys": unknown})
        normalized_refs: dict[str, str] = {}
        blockers: list[str] = []
        for key, value in refs.items():
            if not isinstance(value, str) or not value or len(value) > 255:
                raise WorkflowError(f"{key} must be a bounded non-empty version identifier")
            normalized_refs[key] = value
        for key in _REQUIRED_VERSION_KEYS:
            if key not in normalized_refs:
                blockers.append(key.removesuffix("_version_id") + "_version_required")
        if "project_id" not in normalized_refs:
            blockers.append("project_required")
        for key, value in normalized_refs.items():
            if key == "project_id":
                if self.storage.get_project(value, actor.workspace_id) is None:
                    raise NotFound()
                continue
            try:
                version = self.versions.get(value, actor)
            except NotFound:
                raise WorkflowError(f"{key} was not found in this workspace", code="version_not_found", status=404) from None
            if key == "source_version_id":
                readiness = version.content.get("readiness")
                if readiness != "executable":
                    blockers.append("source_runtime_not_ready")
            if key == "target_version_id":
                if version.kind != "target":
                    blockers.append("target_version_invalid")
                verification = version.content.get("verification") or {}
                if verification.get("state") != "verified":
                    blockers.append("target_verification_required")
                else:
                    expires_at = verification.get("expires_at")
                    try:
                        stale = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
                    except (TypeError, ValueError):
                        stale = True
                    if stale:
                        blockers.append("target_verification_stale")

        normalized_limits = json.loads(_canonical(dict(limits)))
        tier = normalized_limits.get("tier")
        if tier is not None and tier not in _TIERS:
            blockers.append("unsupported_tier")
        repeats = normalized_limits.get("repeats", 1)
        if type(repeats) is not int or not 1 <= repeats <= 5:
            blockers.append("repeats_out_of_range")
        budget = normalized_limits.get("budget_usd_micros", 0)
        if type(budget) is not int or budget < 0:
            blockers.append("budget_invalid")

        content = {"version_refs": normalized_refs, "limits": normalized_limits}
        digest = _digest(content)
        record = self.storage.create_run_plan(
            workspace_id=actor.workspace_id,
            plan_id=uuid.uuid4().hex,
            content_digest=digest,
            state="validated" if not blockers else "blocked",
            content=content,
            blockers=blockers,
            actor_id=actor.actor_id,
        )
        return _plan(record)

    def get_plan(self, actor: Actor, plan_id: str) -> RunPlan:
        actor.require()
        record = self.storage.get_run_plan(plan_id, actor.workspace_id)
        if record is None:
            raise NotFound()
        return _plan(record)

    def authorize(self, actor: Actor, plan_id: str, plan_hash: str, *, ttl_seconds: int = 600) -> Authorization:
        actor.require(write=True)
        if not isinstance(plan_hash, str) or len(plan_hash) != 64:
            raise WorkflowError("plan_hash must be a SHA-256 digest")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise WorkflowError("authorization TTL must be between 1 and 3600 seconds")
        plan = self.get_plan(actor, plan_id)
        if plan.content_digest != plan_hash:
            raise WorkflowError("authorization does not match the current plan", code="authorization_hash_mismatch", status=409)
        if plan.state != "validated":
            raise WorkflowError("run plan has readiness blockers", code="run_plan_not_ready", status=409,
                                details={"blockers": list(plan.blockers)})
        created = datetime.now(timezone.utc)
        record = self.storage.create_run_authorization(
            workspace_id=actor.workspace_id,
            authorization_id=uuid.uuid4().hex,
            plan_id=plan.plan_id,
            plan_hash=plan_hash,
            actor_id=actor.actor_id,
            state="authorized",
            expires_at=(created + timedelta(seconds=ttl_seconds)).isoformat(),
            created_at=created.isoformat(),
        )
        return _authorization(record)

    def enqueue_run(self, worker, actor: Actor, plan_id: str, authorization_id: str,
                    plan_hash: str, idempotency_key: str):
        actor.require(write=True)
        plan = self.get_plan(actor, plan_id)
        if plan.content_digest != plan_hash:
            raise WorkflowError("run submission does not match the plan", code="authorization_hash_mismatch", status=409)
        authorization = self.storage.get_run_authorization(authorization_id, actor.workspace_id)
        if authorization is None or authorization.plan_id != plan_id:
            raise NotFound()
        if authorization.plan_hash != plan_hash:
            raise WorkflowError("authorization is bound to another plan", code="authorization_hash_mismatch", status=409)
        if authorization.state != "authorized":
            raise WorkflowError("authorization is not active", code="authorization_unavailable", status=409)
        if datetime.fromisoformat(authorization.expires_at) <= datetime.now(timezone.utc):
            self.storage.expire_run_authorization(authorization_id, actor.workspace_id)
            raise WorkflowError("authorization has expired", code="authorization_expired", status=409)
        return worker.queue(actor).enqueue({
            "kind": "evaluation_run",
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "authorization_id": authorization_id,
        }, idempotency_key)
