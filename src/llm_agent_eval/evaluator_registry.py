"""Versioned, reviewed custom evaluator artifacts.

Custom evaluators intentionally do not extend :class:`EvaluationSpec`.  An
evaluation version carries an explicit metric-id to evaluator-version binding;
the run planner resolves and freezes those bindings before a worker can run.
The artifact is ordinary operator-supplied code and is only ever mounted into
the dedicated evaluator sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tempfile
from typing import Any

from .artifacts import ArtifactStore
from .auth import Actor
from .contracts import NotFound, WorkflowError, VersionRecord
from .ingestion import inspect_zip
from .runtime.sandbox import SandboxDenied, snapshot_digest
from .storage import Storage, _now
from .versions import VersionStore


_IMAGE_DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}")
_EVALUATOR_STATES = frozenset({"draft", "approved"})


@dataclass(frozen=True)
class ResolvedCustomEvaluator:
    version_id: str
    image_digest: str
    source_dir: Path
    entrypoint: tuple[str, ...]
    score_range: tuple[float, float]
    pass_threshold: float


@dataclass(frozen=True)
class CustomEvaluatorExecution:
    version_id: str
    pass_threshold: float
    evaluate: Any


class CustomEvaluatorRegistry:
    """Create, approve and materialize immutable evaluator versions."""

    def __init__(self, storage: Storage, artifact_root: Path, actor: Actor):
        self.storage = storage
        self.artifact_root = Path(artifact_root).resolve()
        self.actor = actor
        self.versions = VersionStore(storage)

    def create(self, project_id: str, content: dict[str, Any], expected_revision: int) -> VersionRecord:
        self.actor.require(write=True)
        normalized = self.validate_definition(content)
        normalized["review"] = {"state": "draft"}
        return self.versions.create("evaluator", project_id, normalized, expected_revision, self.actor)

    def approve(self, version_id: str) -> VersionRecord:
        self.actor.require(write=True)
        version = self.versions.get(version_id, self.actor)
        if version.kind != "evaluator":
            raise NotFound()
        current = self.validate_definition(version.content)
        review = version.content.get("review") or {}
        state = review.get("state")
        if state == "approved":
            return version
        if state != "draft":
            raise WorkflowError("evaluator review state is invalid", code="evaluator_review_invalid")
        current["review"] = {
            "state": "approved",
            "reviewed_by": self.actor.actor_id,
            "reviewed_at": _now(),
        }
        return self.versions.create("evaluator", version.parent_id, current, version.revision, self.actor)

    def resolve(self, version_id: str) -> ResolvedCustomEvaluator:
        self.actor.require()
        version = self.versions.get(version_id, self.actor)
        if version.kind != "evaluator":
            raise NotFound()
        content = self.validate_definition(version.content)
        review = content["review"]
        if review["state"] != "approved":
            raise WorkflowError(
                "custom evaluator must be explicitly approved before execution",
                code="evaluator_not_approved", status=409,
            )
        artifact_id = content["artifact_ids"][0]
        archive = ArtifactStore(self.storage, self.artifact_root, self.actor).get(
            self.actor.workspace_id, artifact_id
        )
        inspected = inspect_zip(archive)
        root = (self.artifact_root / "ws" / self.actor.workspace_id / "runtime" /
                "evaluators" / version.version_id / inspected.manifest["content_digest"]).resolve()
        if not root.is_relative_to(self.artifact_root):
            raise WorkflowError("evaluator staging path escaped artifact root", code="evaluator_staging_invalid", status=500)
        if not root.is_dir():
            root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="evaluator-stage-", dir=root.parent) as temporary:
                staging = Path(temporary) / "source"
                staging.mkdir(mode=0o700)
                for name, body in inspected.files:
                    destination = (staging / name).resolve()
                    if not destination.is_relative_to(staging):
                        raise WorkflowError("evaluator archive escaped staging root", code="evaluator_archive_invalid", status=422)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as handle:
                        handle.write(body)
                staging_digest = snapshot_digest(staging)
                if not staging_digest.get("files"):
                    raise WorkflowError("evaluator archive is empty", code="evaluator_archive_invalid", status=422)
                root = root.resolve()
                root.parent.mkdir(parents=True, exist_ok=True)
                import os
                os.replace(staging, root)
        try:
            snapshot_digest(root)
        except (OSError, SandboxDenied) as exc:
            raise WorkflowError("evaluator source is unavailable", code="evaluator_source_invalid", status=409) from exc
        return ResolvedCustomEvaluator(
            version_id=version.version_id,
            image_digest=content["image_digest"],
            source_dir=root,
            entrypoint=tuple(content["entrypoint"]),
            score_range=tuple(content["score_range"]),
            pass_threshold=content["pass_threshold"],
        )

    def execution(self, version_id: str, *, sandbox=None) -> CustomEvaluatorExecution:
        resolved = self.resolve(version_id)
        if sandbox is None:
            from .runtime.custom_evaluator import CustomEvaluatorSandbox
            sandbox = CustomEvaluatorSandbox()

        def evaluate(evidence: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
            return sandbox.evaluate(
                image=resolved.image_digest,
                source_dir=resolved.source_dir,
                entrypoint=resolved.entrypoint,
                evidence=evidence,
                reference=reference,
            )

        return CustomEvaluatorExecution(
            version_id=resolved.version_id,
            pass_threshold=resolved.pass_threshold,
            evaluate=evaluate,
        )

    @staticmethod
    def validate_definition(content: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise WorkflowError("evaluator definition must be an object")
        artifact_ids = content.get("artifact_ids")
        if not isinstance(artifact_ids, list) or len(artifact_ids) != 1 or not isinstance(artifact_ids[0], str):
            raise WorkflowError("custom evaluator requires one immutable artifact", code="evaluator_artifact_required")
        image = content.get("image_digest")
        if not isinstance(image, str) or _IMAGE_DIGEST.fullmatch(image) is None:
            raise WorkflowError("custom evaluator image_digest must be immutable", code="evaluator_image_digest_required")
        entrypoint = content.get("entrypoint")
        if (not isinstance(entrypoint, list) or not entrypoint or
                any(not isinstance(item, str) or not item or "\x00" in item for item in entrypoint) or
                not entrypoint[0].startswith("/")):
            raise WorkflowError("custom evaluator entrypoint must be an absolute argv", code="evaluator_entrypoint_invalid")
        score_range = content.get("score_range")
        if (not isinstance(score_range, list) or len(score_range) != 2 or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in score_range) or score_range[0] >= score_range[1]):
            raise WorkflowError("custom evaluator score_range must be two increasing finite numbers", code="evaluator_score_range_invalid")
        threshold = content.get("pass_threshold")
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or
                not math.isfinite(threshold) or not score_range[0] <= threshold <= score_range[1]):
            raise WorkflowError("custom evaluator pass_threshold must be inside score_range", code="evaluator_pass_threshold_invalid")
        review = content.get("review") or {"state": "draft"}
        if not isinstance(review, dict) or review.get("state") not in _EVALUATOR_STATES:
            raise WorkflowError("custom evaluator review state must be draft or approved", code="evaluator_review_invalid")
        return {
            "name": content.get("name", "custom evaluator"),
            "artifact_ids": [artifact_ids[0]],
            "image_digest": image,
            "entrypoint": list(entrypoint),
            "score_range": [score_range[0], score_range[1]],
            "pass_threshold": threshold,
            "review": dict(review),
        }
