"""Durable envelopes; domain-specific authoring belongs in its own service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

VersionKind = Literal["source", "knowledge", "evaluation", "dataset", "target", "connection", "dashboard", "model_profile", "model_selection", "judge_rubric"]
VERSION_KINDS = frozenset(("source", "knowledge", "evaluation", "dataset", "target", "connection", "dashboard", "model_profile", "model_selection", "judge_rubric"))


class WorkflowError(Exception):
    def __init__(self, message: str, *, code: str = "validation_error", status: int = 422, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details or {}


class NotFound(WorkflowError):
    def __init__(self):
        super().__init__("Object not found", code="not_found", status=404)


class RevisionConflict(WorkflowError):
    def __init__(self, revision: int):
        super().__init__("Definition changed; reload before saving", code="conflict", status=409,
                         details={"current_revision": revision})


@dataclass(frozen=True)
class VersionRecord:
    version_id: str
    workspace_id: str
    kind: str
    parent_id: str
    previous_version_id: str | None
    content_digest: str
    revision: int
    actor_id: str
    created_at: str
    content: dict[str, Any]
    authoring_provenance: dict[str, Any]


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    workspace_id: str
    checksum: str
    size_bytes: int
    media_type: str
    created_at: str
    state: str = "ready"


@dataclass(frozen=True)
class VerificationRecord:
    state: str
    capabilities: dict[str, str]
    outcome: str | None = None
    observed_identity: dict[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()
    remote_uncertainty: str | None = None


@dataclass(frozen=True)
class InvocationResult:
    outcome: str
    output: Any
    capabilities: dict[str, str]
    connector_observations: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    remote_uncertainty: str | None = None
    remote_cost: str = "unknown"
    trace_events: tuple[Any, ...] = ()


@dataclass(frozen=True)
class CancellationResult:
    state: str
    invocation_id: str
    observed: bool = False
