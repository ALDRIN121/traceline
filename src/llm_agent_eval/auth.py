"""Trusted request identity and application roles for a single-tenant deployment.

Resolvers are supplied by the deployment/authentication boundary. Request JSON
and workspace headers never authenticate a user or choose a workspace. The
default identity is the explicit local, single-user prototype mode.
"""

from dataclasses import dataclass
import hmac
from pathlib import Path
import secrets
from typing import Callable
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


def create_install_auth_resolver(token_path: Path, workspace_id: str) -> Callable:
    """Create the single-owner bearer boundary for a self-hosted install.

    The token is generated once with restrictive permissions and is never
    returned by the HTTP API. Deployments may replace this resolver with an
    identity-provider integration at application construction.
    """
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
        try:
            path.chmod(0o600)
        except OSError:
            pass
    if not token:
        raise RuntimeError("install owner token is empty")

    def resolve(request):
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not presented or not hmac.compare_digest(presented, token):
            raise WorkflowError("Authentication required", code="unauthorized", status=401)
        return Actor("install-owner", workspace_id, "owner")

    return resolve
