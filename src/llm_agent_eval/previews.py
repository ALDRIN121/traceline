"""Deterministic synthetic dashboard previews from declared spec ranges."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import sysconfig
from typing import Any

from .artifacts import ArtifactStore
from .auth import Actor
from .contracts import NotFound, WorkflowError
from .dashboard import DefinitionValidationError, canonicalize_dashboard, validate_definition
from .jobs import JobQueue
from .storage import Storage
from .versions import VersionStore, canonical_json

GENERATOR_VERSION = "1"
PREVIEW_BANNER = "Synthetic preview — no agent has been evaluated"


def _skill_path() -> Path:
    relative = Path("skills/dashboard-authoring/SKILL.md")
    source = Path(__file__).resolve().parents[2] / relative
    installed = Path(sysconfig.get_path("data")) / "share/llm-agent-eval" / relative
    return source if source.is_file() else installed


def load_skill() -> str:
    path = _skill_path()
    if not path.is_file():
        raise WorkflowError("Built-in dashboard skill is missing")
    return path.read_text(encoding="utf-8")


def _sample(lo: float, hi: float, *parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    return lo + unit * (hi - lo)


def declared_numeric_range(metric: dict[str, Any]) -> tuple[float, float] | None:
    scoring = metric.get("scoring") or {}
    if scoring.get("type") != "numeric":
        return None
    rng = scoring.get("range")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    return float(rng[0]), float(rng[1])


def build_preview_data(definition: dict[str, Any], spec: dict[str, Any], *,
                       spec_digest: str, generator_version: str) -> dict[str, Any]:
    metrics = {item["metric_id"]: item for item in spec.get("metrics") or []}
    blocks = []
    for block in definition.get("blocks") or []:
        metric_id = (block.get("bind") or {}).get("metric")
        metric = metrics.get(metric_id) if isinstance(metric_id, str) else None
        rng = declared_numeric_range(metric) if metric else None
        if metric and rng:
            sample = _sample(rng[0], rng[1], spec_digest, generator_version, metric_id)
            blocks.append({
                "component": block["component"], "metric_id": metric_id,
                "sample": sample, "placeholder": False, "verdict": None,
            })
        elif metric:
            blocks.append({
                "component": block["component"], "metric_id": metric_id,
                "sample": None, "placeholder": True, "verdict": None,
                "reason": "no declared numeric range",
            })
        else:
            blocks.append({
                "component": block["component"], "placeholder": True, "verdict": None,
                "reason": "synthetic preview has no measured run",
            })
    payload = {
        "data_kind": "synthetic",
        "gate_result": None,
        "banner": PREVIEW_BANNER,
        "generator_version": generator_version,
        "blocks": blocks,
    }
    payload["data_hash"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return payload


def render_preview_html(data: dict[str, Any]) -> str:
    rows = []
    for block in data["blocks"]:
        metric = html.escape(str(block.get("metric_id") or block.get("component")))
        if block.get("placeholder"):
            body = "placeholder"
        else:
            body = html.escape(f"{block['sample']:.4f}")
        rows.append(f"<li><strong>{metric}</strong>: {body}</li>")
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(PREVIEW_BANNER)}</title></head><body>"
        f"<p role='status'>{html.escape(PREVIEW_BANNER)}</p>"
        f"<ul>{''.join(rows)}</ul></body></html>"
    )


class PreviewService:
    def __init__(self, storage: Storage, artifact_root):
        self.storage, self.artifact_root = storage, Path(artifact_root)

    def create_version(self, actor: Actor, dashboard_id: str, definition: dict, expected_revision: int):
        actor.require(write=True)
        try:
            validate_definition(definition)
        except DefinitionValidationError as exc:
            raise WorkflowError(str(exc), details={"problems": [p.__dict__ for p in exc.problems]}) from exc
        content = {"definition": canonicalize_dashboard(definition), "skill": "dashboard-authoring"}
        version = VersionStore(self.storage).create("dashboard", dashboard_id, content, expected_revision, actor)
        from dataclasses import asdict
        return {"state": "validated", "version": asdict(version)}

    def submit(self, actor: Actor, dashboard_version_id: str, refs: dict, idempotency_key: str):
        actor.require(write=True)
        VersionStore(self.storage).get(dashboard_version_id, actor)
        command = {
            "kind": "dashboard_preview",
            "dashboard_version_id": dashboard_version_id,
            "evaluation_version_id": refs.get("evaluation_version_id"),
            "dataset_version_id": refs.get("dataset_version_id"),
            "generator_version": refs.get("generator_version") or GENERATOR_VERSION,
        }
        if not command["evaluation_version_id"] or not command["dataset_version_id"]:
            raise WorkflowError("evaluation_version_id and dataset_version_id are required")
        if command["generator_version"] != GENERATOR_VERSION:
            raise WorkflowError("Unsupported generator_version")
        return JobQueue(self.storage, actor, fingerprint_key=hashlib.sha256(
            ("preview:" + str(self.artifact_root.resolve())).encode()
        ).digest()).enqueue(command, idempotency_key)

    def execute(self, actor: Actor, command: dict) -> dict[str, Any]:
        load_skill()  # uploaded SKILL.md never reaches this path
        dashboard = VersionStore(self.storage).get(command["dashboard_version_id"], actor)
        evaluation = VersionStore(self.storage).get(command["evaluation_version_id"], actor)
        dataset = VersionStore(self.storage).get(command["dataset_version_id"], actor)
        if dashboard.kind != "dashboard" or evaluation.kind != "evaluation" or dataset.kind != "dataset":
            raise WorkflowError("Preview refs must be dashboard, evaluation, and dataset versions")
        definition = canonicalize_dashboard(dashboard.content.get("definition") or dashboard.content)
        validate_definition(definition)
        spec = evaluation.content.get("spec") or evaluation.content
        spec_digest = evaluation.content_digest
        data = build_preview_data(
            definition, spec, spec_digest=spec_digest, generator_version=command["generator_version"],
        )
        receipt = {
            "presentation_only": True,
            "dashboard_version_id": dashboard.version_id,
            "evaluation_version_id": evaluation.version_id,
            "dataset_version_id": dataset.version_id,
            "generator_version": command["generator_version"],
        }
        manifest = {
            "data_kind": "synthetic",
            "gate_result": None,
            "data_hash": data["data_hash"],
            "definition_hash": hashlib.sha256(canonical_json(definition).encode()).hexdigest(),
            "review_receipt": receipt,
            "banner": PREVIEW_BANNER,
            "skill": "dashboard-authoring",
        }
        html_doc = render_preview_html(data)
        artifacts = ArtifactStore(self.storage, self.artifact_root, actor)
        definition_art = artifacts.put(actor.workspace_id, canonical_json(definition).encode(), "application/json")
        data_art = artifacts.put(actor.workspace_id, canonical_json(data).encode(), "application/json")
        html_art = artifacts.put(actor.workspace_id, html_doc.encode(), "text/html")
        manifest_art = artifacts.put(actor.workspace_id, canonical_json(manifest).encode(), "application/json")
        return {
            "state": "validated",
            "data_kind": "synthetic",
            "data_hash": data["data_hash"],
            "gate_result": None,
            "blocks": data["blocks"],
            "generator_version": command["generator_version"],
            "review_receipt": receipt,
            "html_artifact_id": html_art.artifact_id,
            "artifact_ids": [
                definition_art.artifact_id, data_art.artifact_id,
                html_art.artifact_id, manifest_art.artifact_id,
            ],
        }
