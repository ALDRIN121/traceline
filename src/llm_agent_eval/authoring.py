"""Typed evaluation proposals: validate, repair twice, never keyword-replace specs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import Actor
from .config import settings
from .contracts import WorkflowError
from .gateway import GatewayError, ModelGateway
from .sessions import SessionStore
from .spec import validate_spec
from .versions import VersionStore


class MetricPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str
    scoring_condition: str | None = None


class TurnProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    patches: list[MetricPatch] = Field(default_factory=list)
    selected_metric_ids: list[str] = Field(default_factory=list)


class AuthoringService:
    def __init__(self, storage, gateway: ModelGateway):
        self.storage, self.gateway = storage, gateway

    def execute(self, actor: Actor, command: dict) -> dict:
        return self.publish(actor, command, self.prepare(actor, command))

    def prepare(self, actor: Actor, command: dict) -> dict:
        """Call the provider outside publication transactions; bind to read revision."""
        session = SessionStore(self.storage).get(actor, command["session_id"])
        if session["revision"] != command["expected_revision"]:
            from .contracts import RevisionConflict
            raise RevisionConflict(session["revision"])
        versions = VersionStore(self.storage)
        current = versions.get(session["evaluation_version_id"], actor)
        spec = dict(current.content["spec"])
        prior_version_id = current.version_id
        messages = [
            {"role": "system", "content": (
                "Return one JSON object for the evaluation turn. "
                "Patch only named metrics. Never invent labels or verification."
            )},
            {"role": "user", "content": json.dumps({
                "message": command.get("message"),
                "card_action": command.get("card_action"),
                "evaluation": spec,
                "knowledge_version_id": session.get("knowledge_version_id"),
            }, sort_keys=True)},
        ]
        proposal, repairs = None, 0
        last_error = None
        try:
            while True:
                raw = self.gateway.chat_json(messages, json_schema_hint=TurnProposal.model_json_schema())
                try:
                    proposal = TurnProposal.model_validate(raw)
                    spec = _apply_proposal(spec, proposal)
                    validated = validate_spec(spec)
                    break
                except (ValidationError, ValueError) as exc:
                    last_error = str(exc)
                    repairs += 1
                    if repairs > settings.harness.spec_repair_attempts:
                        return {
                            "evaluation_id": session["evaluation_id"],
                            "evaluation_version_id": prior_version_id,
                            "state": "rejected",
                            "verification_id": None,
                            "proposal": raw if isinstance(raw, dict) else {},
                            "repairs": repairs,
                        }
                    messages.append({"role": "assistant", "content": json.dumps(raw)})
                    messages.append({"role": "user", "content": f"Validation failed: {last_error}"})
        except GatewayError:
            return {
                "evaluation_id": session["evaluation_id"],
                "evaluation_version_id": prior_version_id,
                "state": "failed",
                "verification_id": None,
                "proposal": {},
            }

        return {
            "evaluation_id": session["evaluation_id"],
            "evaluation_version_id": prior_version_id,
            "base_revision": current.revision,
            "state": "prepared",
            "spec": validated.model_dump(mode="json"),
            "proposal": proposal.model_dump(),
            "repairs": repairs,
        }

    def publish(self, actor: Actor, command: dict, prepared: dict) -> dict:
        if prepared["state"] != "prepared":
            return prepared
        versions = VersionStore(self.storage)
        with self.storage.workspace_transaction(actor.workspace_id):
            session = SessionStore(self.storage).get(actor, command["session_id"])
            if (session["revision"] != command["expected_revision"]
                    or session["evaluation_version_id"] != prepared["evaluation_version_id"]):
                from .contracts import RevisionConflict
                raise RevisionConflict(session["revision"])
            record = versions.create(
                "evaluation", session["evaluation_id"], {"spec": prepared["spec"]},
                prepared["base_revision"], actor,
            )
            SessionStore(self.storage).advance(
                actor, session["session_id"], command["expected_revision"],
                evaluation_version_id=record.version_id,
            )
        return {
            "evaluation_id": session["evaluation_id"],
            "evaluation_version_id": record.version_id,
            "state": "validated",
            "verification_id": None,
            "proposal": prepared["proposal"],
            "repairs": prepared["repairs"],
        }


def _apply_proposal(spec: dict[str, Any], proposal: TurnProposal) -> dict[str, Any]:
    if proposal.action == "no_op":
        raise ValueError("proposal made no evaluation change")
    updated = dict(spec)
    metrics = [dict(metric) for metric in spec["metrics"]]
    if proposal.action == "patch_metrics":
        by_id = {patch.metric_id: patch for patch in proposal.patches}
        if not by_id:
            raise ValueError("patch_metrics requires patches")
        new_metrics = []
        for metric in metrics:
            item = dict(metric)
            patch = by_id.get(item["metric_id"])
            if patch and patch.scoring_condition:
                scoring = dict(item.get("scoring") or {})
                scoring["condition"] = patch.scoring_condition
                item["scoring"] = scoring
            new_metrics.append(item)
        updated["metrics"] = new_metrics
        return updated
    if proposal.action == "select_metrics":
        selected = set(proposal.selected_metric_ids)
        if not selected:
            raise ValueError("select_metrics requires selected_metric_ids")
        updated["metrics"] = [metric for metric in metrics if metric["metric_id"] in selected]
        return updated
    raise ValueError(f"unsupported proposal action {proposal.action}")
