"""Trusted request identity and application roles for a single-tenant deployment.

Resolvers are supplied by the deployment/authentication boundary. Request JSON
and workspace headers never authenticate a user or choose a workspace. The
default identity is the explicit local, single-user prototype mode.
"""

from dataclasses import dataclass
from typing import Literal

from .contracts import WorkflowError


@dataclass(frozen=True)
class Actor:
    actor_id: str
    workspace_id: str
    role: Literal["owner", "editor", "viewer"] = "viewer"

    def require(self, *, write: bool = False, workspace_id: str | None = None) -> None:
        if not self.actor_id or not self.workspace_id or self.role not in {"owner", "editor", "viewer"}:
            raise WorkflowError("Authentication required", code="unauthorized", status=401)
        if workspace_id is not None and workspace_id != self.workspace_id:
            raise WorkflowError("Workspace access denied", code="forbidden", status=403)
        if write and self.role not in {"owner", "editor"}:
            raise WorkflowError("Editing permission required", code="forbidden", status=403)
