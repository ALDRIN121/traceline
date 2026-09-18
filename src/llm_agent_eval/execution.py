"""Durable worker execution for frozen run plans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .engine import Engine
from .runtime.source import SourceRuntimeService
from .spec import validate_spec
from .storage import Storage
from .targets.http_json import HttpJsonAdapter
from .targets.local import LocalTargetAdapter
from .targets.network_policy import EndpointPolicy
from .versions import VersionStore


class RunExecutionService:
    """Resolve a plan inside the worker and run it exactly once by key."""

    def __init__(self, storage: Storage, artifact_root, *, target_factory: Callable | None = None):
        self.storage = storage
        self.artifact_root = artifact_root
        self.target_factory = target_factory
        self.versions = VersionStore(storage)

    def execute(self, actor: Actor, command: dict[str, Any], context) -> dict[str, Any]:
        actor.require(write=True)
        plan = self.storage.get_run_plan(command.get("plan_id"), actor.workspace_id)
        if plan is None:
            raise NotFound()
        if plan.content_digest != command.get("plan_hash"):
            raise WorkflowError("run plan hash changed", code="authorization_hash_mismatch", status=409)
        authorization = self.storage.get_run_authorization(
            command.get("authorization_id"), actor.workspace_id
        )
        if (authorization is None or authorization.plan_id != plan.plan_id
                or authorization.plan_hash != plan.content_digest
                or authorization.state != "authorized"):
            raise WorkflowError("run authorization is unavailable", code="authorization_unavailable", status=409)
        if datetime.fromisoformat(authorization.expires_at) <= datetime.now(timezone.utc):
            self.storage.expire_run_authorization(authorization.authorization_id, actor.workspace_id)
            raise WorkflowError("run authorization has expired", code="authorization_expired", status=409)
        if plan.state != "validated":
            raise WorkflowError("run plan has readiness blockers", code="run_plan_not_ready", status=409)

        refs = plan.content["version_refs"]
        evaluation = self.versions.get(refs["evaluation_version_id"], actor)
        dataset = self.versions.get(refs["dataset_version_id"], actor)
        if evaluation.kind != "evaluation" or dataset.kind != "dataset":
            raise WorkflowError("run plan version kinds do not match", code="run_plan_invalid", status=409)
        spec_payload = evaluation.content.get("spec")
        cases = dataset.content.get("cases")
        if not isinstance(spec_payload, dict) or not isinstance(cases, list):
            raise WorkflowError("evaluation or dataset version is not executable", code="definition_not_ready", status=409)
        frozen_spec = dict(spec_payload)
        frozen_spec["cases"] = cases
        frozen_spec["dataset_version"] = refs["dataset_version_id"]
        spec = validate_spec(frozen_spec)

        adapter, manifest, entrypoint = self._resolve_target(actor, refs, context)
        idempotency_key = f"evaluation-plan:{plan.plan_id}:{plan.content_digest}"
        existing = self.storage.get_run_by_idempotency_key(actor.workspace_id, idempotency_key)
        if existing is not None and existing.status in {"complete", "failed", "cancelled"}:
            return {"state": existing.status, "run_id": existing.run_id, "plan_id": plan.plan_id}
        limits = plan.content.get("limits") or {}
        engine = Engine(
            self.storage, target_adapter=adapter,
            work_root=self.artifact_root / "ws" / actor.workspace_id / "runs",
        )
        run = existing or engine.create_run(
            actor.workspace_id, spec, tier=limits.get("tier") or spec.run_tier or "quick",
            repeats=limits.get("repeats", 1), retry_max=limits.get("retry_max", 0),
            entrypoint=entrypoint or ("/bin/true",),
            idempotency_key=idempotency_key,
            created_by=actor.actor_id,
            budget_usd_micros=limits.get("budget_usd_micros", 0),
            concurrency=limits.get("concurrency", 1),
            timeout_seconds=limits.get("timeout_seconds", 120),
            invocation_manifest=manifest,
            world_config={"run_plan_id": plan.plan_id, "plan_hash": plan.content_digest},
        )
        result = engine.run(run.run_id, actor.workspace_id,
                            should_cancel=context.cancellation_event.is_set)
        return {"state": result.status, "run_id": result.run_id, "plan_id": plan.plan_id}

    def _resolve_target(self, actor: Actor, refs: dict[str, str], context):
        if self.target_factory is not None:
            return self.target_factory(actor, refs, context)
        if refs.get("source_version_id"):
            source = self.versions.get(refs["source_version_id"], actor)
            if source.kind != "source" or source.content.get("readiness") != "executable":
                raise WorkflowError("source runtime is not executable", code="source_runtime_not_ready", status=409)
            runtime = source.content.get("runtime") or {}
            if not runtime.get("image_digest") or not runtime.get("entrypoint"):
                raise WorkflowError("source runtime manifest is incomplete", code="runtime_manifest_invalid", status=409)
            snapshot = SourceRuntimeService(self.storage, self.artifact_root, actor).materialize(source.version_id)
            manifest = {
                "image": runtime["image_digest"], "entrypoint": runtime["entrypoint"],
                "source_dir": str(snapshot.source_dir),
                "timeout_seconds": 120, "egress": "none",
            }
            return LocalTargetAdapter(), manifest, tuple(runtime["entrypoint"])
        if refs.get("target_version_id"):
            target = self.versions.get(refs["target_version_id"], actor)
            if target.kind != "target":
                raise WorkflowError("target version is invalid", code="target_version_invalid", status=409)
            return HttpJsonAdapter(EndpointPolicy()), {"target": target.content, "retries": 0}, ("/bin/true",)
        raise WorkflowError("run plan has no executable target", code="execution_target_required", status=409)
