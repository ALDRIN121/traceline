"""Durable worker execution for frozen run plans."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Callable

from .auth import Actor
from .calibration import PersistentJudgeReadinessRegistry
from .contracts import NotFound, WorkflowError
from .engine import Engine
from .gateway import LiteLLMGateway
from .judge_gateway import GatewayJudge
from .profiles import ProfileStore
from .rubric_store import RubricStore
from .runtime.source import SourceRuntimeService
from .secrets import SecretStore
from .spec import validate_spec
from .storage import Storage
from .targets.http_json import HttpJsonAdapter
from .targets.http_stream import HttpStreamAdapter
from .targets.http_job import HttpJobAdapter
from .targets.http_session import HttpSessionAdapter
from .targets.local import LocalTargetAdapter
from .targets.network_policy import EndpointPolicy
from .versions import VersionStore


class RunExecutionService:
    """Resolve a plan inside the worker and run it exactly once by key."""

    def __init__(self, storage: Storage, artifact_root, *, target_factory: Callable | None = None,
                 judge_gateway=None, proxy_session_factory: Callable | None = None):
        self.storage = storage
        self.artifact_root = artifact_root
        self.target_factory = target_factory
        self.judge_gateway = judge_gateway
        self.proxy_session_factory = proxy_session_factory
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

        resolved = self._resolve_target(actor, refs, context)
        if len(resolved) == 3:
            adapter, manifest, entrypoint = resolved
            target_context = {}
        else:
            adapter, manifest, entrypoint, target_context = resolved
        idempotency_key = f"evaluation-plan:{plan.plan_id}:{plan.content_digest}"
        existing = self.storage.get_run_by_idempotency_key(actor.workspace_id, idempotency_key)
        if existing is not None and existing.status in {"complete", "failed", "cancelled"}:
            return {"state": existing.status, "run_id": existing.run_id, "plan_id": plan.plan_id}
        limits = plan.content.get("limits") or {}
        judge_gateway = self._resolve_judge_gateway(actor, refs)
        engine = Engine(
            self.storage, target_adapter=adapter,
            work_root=self.artifact_root / "ws" / actor.workspace_id / "runs",
            target_execution_context=target_context,
            judge_readiness=PersistentJudgeReadinessRegistry(self.storage, actor),
        )
        if judge_gateway is not None:
            engine.judge_factory = self._judge_factory(
                actor, engine, judge_gateway, engine.judge_readiness,
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
            world_config={
                "run_plan_id": plan.plan_id,
                "plan_hash": plan.content_digest,
                "version_refs": dict(refs),
            },
        )
        proxy_session = None
        if manifest.get("egress") == "proxy":
            if self.proxy_session_factory is None:
                raise WorkflowError(
                    "proxy egress is configured but no trusted proxy session is available",
                    code="proxy_session_unavailable", status=409,
                )
            proxy_session = self.proxy_session_factory(
                actor, run.run_id, plan, context,
            )
            session_info = proxy_session.start()
            engine.target_execution_context.update({
                "proxy_endpoint": session_info.proxy_endpoint,
                "network_name": session_info.network_name,
                "network_run_id": session_info.network_run_id,
                "ca_cert": session_info.ca_cert,
            })
        try:
            result = engine.run(run.run_id, actor.workspace_id,
                                should_cancel=context.cancellation_event.is_set)
        finally:
            if proxy_session is not None:
                proxy_session.stop()
        return {"state": result.status, "run_id": result.run_id, "plan_id": plan.plan_id}

    def _resolve_judge_gateway(self, actor: Actor, refs: dict[str, str]):
        selection_id = refs.get("model_selection_version_id")
        if not selection_id:
            return self.judge_gateway
        profile = ProfileStore(self.storage).selected(selection_id, "judge", actor)
        return LiteLLMGateway.from_profile(
            profile,
            resolve_secret=lambda secret_ref: SecretStore(
                self.storage, actor, self.artifact_root / "install-secret.key"
            ).resolve(secret_ref),
        )

    def _judge_factory(self, actor: Actor, engine: Engine | None, gateway=None, readiness=None):
        """Bind persisted rubrics and the current attempt to the worker gateway."""
        rubric_store = RubricStore(self.storage)
        gateway = gateway or self.judge_gateway

        def factory(binding):
            if engine is None:
                # The temporary factory is replaced immediately after the
                # engine is constructed; this branch is defensive only.
                raise WorkflowError("judge engine context is unavailable", code="judge_unavailable", status=409)
            return GatewayJudge(
                gateway,
                rubric_version=binding.rubric_version,
                schema_version=binding.schema_version,
                readiness=readiness.get(binding) if readiness is not None else None,
                rubric_resolver=lambda rubric_id: rubric_store.get(rubric_id, actor),
                context_resolver=lambda metric, case, evidence: engine.judgment_context(
                    metric, case, evidence
                ),
            )

        return factory

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
                "timeout_seconds": runtime.get("timeout_seconds", 120),
                "egress": runtime.get("egress", "none"),
            }
            return LocalTargetAdapter(), manifest, tuple(runtime["entrypoint"]), {
                "source_dir": str(snapshot.source_dir),
            }
        if refs.get("target_version_id"):
            target = self.versions.get(refs["target_version_id"], actor)
            if target.kind != "target":
                raise WorkflowError("target version is invalid", code="target_version_invalid", status=409)
            verification = target.content.get("verification") or {}
            if verification.get("state") != "verified":
                raise WorkflowError("target verification is required", code="target_verification_required", status=409)
            try:
                expired = datetime.fromisoformat(verification["expires_at"]) <= datetime.now(timezone.utc)
            except (KeyError, TypeError, ValueError):
                expired = True
            if expired:
                raise WorkflowError("target verification has expired", code="target_verification_stale", status=409)
            target_context = {}
            auth = target.content.get("auth") or {"type": "none"}
            if auth.get("type") in {"bearer", "api_key"}:
                target_context["secret"] = SecretStore(
                    self.storage, actor, self.artifact_root / "install-secret.key"
                ).resolve(auth["secret_ref"])
            allowed = {
                item.strip() for item in os.environ.get("EVAL_ENGINE_ALLOW_ENDPOINTS", "").split(",")
                if item.strip()
            }
            adapter = {
                "http_stream": HttpStreamAdapter(policy=EndpointPolicy(allow_exact=allowed)),
                "http_job": HttpJobAdapter(policy=EndpointPolicy(allow_exact=allowed)),
                "http_session": HttpSessionAdapter(policy=EndpointPolicy(allow_exact=allowed)),
            }.get(target.content.get("kind"), HttpJsonAdapter(EndpointPolicy(allow_exact=allowed)))
            return adapter, {
                "target": target.content, "retries": 0,
            }, ("/bin/true",), target_context
        raise WorkflowError("run plan has no executable target", code="execution_target_required", status=409)
