"""Engine-owned per-run Podman network lifecycle for proxy egress."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .sandbox import PodmanSandbox, SandboxDenied

MANAGED_LABEL = "llm-agent-eval.managed"
RUN_LABEL = "llm-agent-eval.run"


@dataclass(frozen=True)
class NetworkLease:
    name: str
    run_id: str
    state: str = "created"


class PodmanRunNetwork:
    """Create an internal, labelled network and remove it on release.

    The proxy listener is reached through Podman's host gateway; provider DNS
    resolution and credentials remain on the trusted outbound leg. The
    network's internal flag prevents a sandbox container from getting a
    second direct external route.
    """

    def __init__(self, sandbox: PodmanSandbox | None = None):
        self.sandbox = sandbox or PodmanSandbox()

    @staticmethod
    def name_for(run_id: str) -> str:
        if not isinstance(run_id, str) or not run_id:
            raise SandboxDenied("run id is required for a managed network")
        return "llm-agent-eval-run-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]

    def create(self, run_id: str) -> NetworkLease:
        name = self.name_for(run_id)
        result = self.sandbox._command([
            "network", "create", "--internal", "--disable-dns",
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{RUN_LABEL}={hashlib.sha256(run_id.encode()).hexdigest()}",
            name,
        ])
        if result.returncode or result.interrupted:
            raise SandboxDenied("managed proxy network could not be created")
        self.validate(name, run_id)
        return NetworkLease(name=name, run_id=run_id)

    def validate(self, name: str, run_id: str) -> None:
        expected = self.name_for(run_id)
        if name != expected:
            raise SandboxDenied("proxy network name is not engine-owned")
        result = self.sandbox._command(["network", "inspect", "--format", "json", name])
        if result.returncode or result.interrupted or result.truncated:
            raise SandboxDenied("proxy network is unavailable")
        try:
            payload = json.loads(result.stdout)
            record = payload[0] if isinstance(payload, list) else payload
            labels = record.get("Labels") or record.get("labels") or {}
            if labels.get(MANAGED_LABEL) != "true" or labels.get(RUN_LABEL) != hashlib.sha256(run_id.encode()).hexdigest():
                raise ValueError
            if record.get("internal") is False or record.get("Internal") is False:
                raise ValueError
        except (ValueError, TypeError, IndexError, KeyError, json.JSONDecodeError):
            raise SandboxDenied("proxy network is not an internal engine-owned network") from None

    def remove(self, lease: NetworkLease) -> dict[str, Any]:
        result = self.sandbox._command(["network", "rm", lease.name])
        if result.returncode and not result.interrupted:
            # A missing network is already clean; any other failure is not.
            if b"no such network" not in result.stderr.lower():
                return {"state": "failed"}
        return {"state": "removed" if not result.interrupted else "failed"}
