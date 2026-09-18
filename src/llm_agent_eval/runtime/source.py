"""Engine-owned source snapshots and executable runtime admission."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from ..artifacts import ArtifactStore
from ..auth import Actor
from ..contracts import NotFound, WorkflowError
from ..ingestion import inspect_zip
from ..storage import Storage
from ..versions import VersionRecord, VersionStore
from .build import BuildService, RuntimeProfile
from .sandbox import SandboxDenied, snapshot_digest


@dataclass(frozen=True)
class SourceSnapshot:
    version_id: str
    source_dir: Path
    digest: str


class SourceRuntimeService:
    """Materialize a verified archive into private worker staging and build it.

    Host paths are worker-owned implementation details. They never enter a run
    plan or an agent-visible manifest; the worker recreates the snapshot from
    the immutable source artifact when it needs it.
    """

    def __init__(self, storage: Storage, artifact_root: Path, actor: Actor,
                 *, build_service: BuildService | None = None):
        self.storage = storage
        self.artifact_root = Path(artifact_root).resolve()
        self.actor = actor
        self.builder = build_service or BuildService()
        self.versions = VersionStore(storage)

    def _version(self, version_id: str) -> VersionRecord:
        version = self.versions.get(version_id, self.actor)
        if version.kind != "source":
            raise NotFound()
        return version

    def materialize(self, version_id: str) -> SourceSnapshot:
        version = self._version(version_id)
        artifact_ids = version.content.get("artifact_ids")
        if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
            raise WorkflowError("Source version must reference one immutable archive",
                                code="source_artifact_invalid", status=409)
        archive = ArtifactStore(self.storage, self.artifact_root, self.actor).get(
            self.actor.workspace_id, artifact_ids[0]
        )
        inspected = inspect_zip(archive)
        root = (self.artifact_root / "ws" / self.actor.workspace_id / "runtime" /
                "sources" / version.version_id / inspected.manifest["content_digest"])
        root = root.resolve()
        if not root.is_relative_to(self.artifact_root):
            raise WorkflowError("Source staging path escaped artifact root", code="source_staging_invalid", status=500)
        if root.is_dir():
            try:
                digest = _snapshot_hash(root)
                return SourceSnapshot(version.version_id, root, digest)
            except (OSError, SandboxDenied):
                shutil.rmtree(root, ignore_errors=True)
        parent = root.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="source-stage-", dir=parent) as temporary:
            staging = Path(temporary) / "source"
            staging.mkdir(mode=0o700)
            for name, body in inspected.files:
                destination = (staging / name).resolve()
                if not destination.is_relative_to(staging):
                    raise WorkflowError("Source archive escaped staging root", code="source_archive_invalid", status=422)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
            digest = _snapshot_hash(staging)
            os.replace(staging, root)
        return SourceSnapshot(version.version_id, root, digest)

    def prepare(self, version_id: str, runtime_profile: dict[str, Any]) -> VersionRecord:
        version = self._version(version_id)
        if not isinstance(runtime_profile, dict):
            raise WorkflowError("runtime_profile is required", code="runtime_profile_required")
        if not runtime_profile.get("base_image") or not runtime_profile.get("entrypoint"):
            raise WorkflowError("runtime_profile is required", code="runtime_profile_required")
        try:
            profile = RuntimeProfile(
                base_image=runtime_profile["base_image"],
                entrypoint=tuple(runtime_profile["entrypoint"]),
                policy_version=runtime_profile.get("policy_version", "r1-restricted-build-v1"),
                egress=runtime_profile.get("egress", "none"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("runtime_profile is invalid", code="runtime_profile_invalid") from exc
        snapshot = self.materialize(version_id)
        job = self.builder.prepare(snapshot.source_dir, profile)
        content = dict(version.content)
        content["readiness"] = "executable"
        content["runtime"] = {
            "image_digest": job.image_digest,
            "entrypoint": list(job.entrypoint),
            "egress": profile.egress,
            "source_snapshot_digest": job.source_digest,
            "build_provenance": dict(job.provenance),
        }
        return self.versions.create("source", version.parent_id, content, version.revision, self.actor)


def _snapshot_hash(directory: Path) -> str:
    try:
        tree = snapshot_digest(directory)
    except (OSError, SandboxDenied) as exc:
        raise WorkflowError("Source snapshot is not safe", code="source_snapshot_invalid", status=422) from exc
    import hashlib, json
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
