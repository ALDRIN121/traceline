"""Honest, narrow preflight for the rootless Podman execution tier (T12a).

This module discovers whether the *runtime setup* is suitable for the later
containment experiment.  It deliberately does not create containers, configure
networking, trust a CA, or certify an egress boundary.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


PODMAN_TIMEOUT_SECONDS = 10.0
SUPPORTED_RUNTIMES = frozenset({"runc", "runsc", "vm"})

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RuntimePreflight:
    """A secret-safe setup result suitable for a future setup card."""

    state: str
    code: str
    message: str
    runtime_tier: str
    next_action: str
    engine_version: str | None = None
    api_version: str | None = None
    connection: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return only fields that were observed; no diagnostic raw output."""
        return {key: value for key, value in asdict(self).items() if value is not None}


def _run_podman(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one fixed Podman argument vector; shell expansion is never involved."""
    return subprocess.run(
        args,
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=timeout,
    )


def _blocked(
    code: str,
    message: str,
    runtime_tier: str,
    next_action: str,
) -> RuntimePreflight:
    return RuntimePreflight(
        state="blocked_setup",
        code=code,
        message=message,
        runtime_tier=runtime_tier,
        next_action=next_action,
    )


def _command_json(
    command: list[str], runner: CommandRunner
) -> tuple[object | None, str | None]:
    try:
        completed = runner(command, timeout=PODMAN_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, subprocess.TimeoutExpired):
        return None, "failed"
    if completed.returncode != 0:
        return None, "failed"
    try:
        return json.loads(completed.stdout), None
    except (TypeError, json.JSONDecodeError):
        return None, "failed"


def _selected_connection(connections: object) -> dict[str, Any] | None:
    if not isinstance(connections, list):
        return None
    for connection in connections:
        if isinstance(connection, dict) and connection.get("Default") is True:
            return connection
    return None


def _socket_override(value: str | None) -> str | None:
    """Return a safe Unix socket endpoint, never a general remote endpoint."""
    if not value:
        return None
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme == "unix" else value
    if (
        (parsed.scheme not in {"", "unix"})
        or not path.startswith("/")
        or ".." in path.split("/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def _is_root_connection(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    parsed = urlparse(endpoint)
    path = parsed.path if parsed.scheme else endpoint
    return (
        parsed.username == "root"
        or path.startswith("/run/podman/")
        or path.startswith("/var/run/podman/")
        or path.startswith("/run/user/0/")
    )


def _machine_is_running(machine: object) -> bool:
    candidates = machine if isinstance(machine, list) else [machine]
    return any(
        isinstance(candidate, dict)
        and (
            candidate.get("Running") is True
            or str(candidate.get("State", "")).lower() == "running"
        )
        for candidate in candidates
    )


def _version(info: dict[str, Any], field: str) -> str | None:
    version = info.get("version")
    value = version.get(field) if isinstance(version, dict) else None
    return str(value) if value else None


def preflight(
    *,
    environment: Mapping[str, str] | None = None,
    system: str | None = None,
    command_runner: CommandRunner = _run_podman,
) -> RuntimePreflight:
    """Inspect Podman setup without selecting Docker or a subprocess fallback."""
    env = os.environ if environment is None else environment
    runtime_tier = env.get("AGENT_RUNTIME", "runc").strip().lower()
    host_system = (system or platform.system()).strip().lower()

    if runtime_tier not in SUPPORTED_RUNTIMES:
        return _blocked(
            "runtime_invalid",
            "AGENT_RUNTIME must be one of runc, runsc, or vm.",
            runtime_tier,
            "Set AGENT_RUNTIME to runc, runsc, or vm; Docker is not a supported runtime.",
        )
    if runtime_tier == "runsc" and host_system != "linux":
        return _blocked(
            "runtime_unsupported",
            "The runsc isolation tier is supported only on Linux.",
            runtime_tier,
            "Use a Linux host for runsc, or select AGENT_RUNTIME=runc for this platform.",
        )
    if runtime_tier == "vm":
        return _blocked(
            "runtime_unsupported",
            "The vm isolation tier is a future Linux-only capability and is not available yet.",
            runtime_tier,
            "Use AGENT_RUNTIME=runc while the vm tier remains unavailable.",
        )

    configured_socket = env.get("ENGINE_SOCKET")
    socket = _socket_override(configured_socket)
    if configured_socket and socket is None:
        return _blocked(
            "runtime_socket_invalid",
            "ENGINE_SOCKET must name an absolute Unix socket endpoint.",
            runtime_tier,
            "Set ENGINE_SOCKET to a rootless Podman Unix socket, or unset it to use the default connection.",
        )
    if _is_root_connection(socket):
        return _blocked(
            "rootless_runtime_required",
            "ENGINE_SOCKET names a root Podman socket.",
            runtime_tier,
            "Configure a rootless Podman user socket; do not use a root or Docker socket.",
        )

    connections, error = _command_json(
        ["podman", "system", "connection", "list", "--format", "json"], command_runner
    )
    if error == "missing":
        return _blocked(
            "runtime_unavailable",
            "Podman is not installed or is not available on PATH.",
            runtime_tier,
            "Install Podman and configure a rootless connection before continuing.",
        )
    connection = _selected_connection(connections)
    if error or connection is None:
        return _blocked(
            "runtime_unavailable",
            "Podman could not report a default connection.",
            runtime_tier,
            "Configure and select a rootless Podman connection, then run preflight again.",
        )
    if _is_root_connection(str(connection.get("URI", ""))):
        return _blocked(
            "rootless_runtime_required",
            "The default Podman connection targets a root socket.",
            runtime_tier,
            "Configure a rootless Podman user connection; do not use a root or Docker socket.",
        )

    if host_system == "darwin":
        machine, error = _command_json(
            ["podman", "machine", "inspect"], command_runner
        )
        if error or not _machine_is_running(machine):
            return _blocked(
                "runtime_unavailable",
                "A running Podman Machine is required on macOS.",
                runtime_tier,
                "Initialize and start a rootless Podman Machine, then run preflight again.",
            )

    info_command = ["podman"]
    if socket:
        info_command.extend(["--url", socket])
    info_command.extend(["info", "--format", "json"])
    info, error = _command_json(info_command, command_runner)
    if error or not isinstance(info, dict):
        return _blocked(
            "runtime_unavailable",
            "Podman could not report engine information for the selected connection.",
            runtime_tier,
            "Start or repair the rootless Podman connection, then run preflight again.",
        )

    host = info.get("host")
    security = host.get("security") if isinstance(host, dict) else None
    if not isinstance(security, dict) or security.get("rootless") is not True:
        return _blocked(
            "rootless_runtime_required",
            "The selected Podman connection is not running rootless.",
            runtime_tier,
            "Configure Podman as an unprivileged user and select its rootless connection.",
        )

    if socket:
        safe_connection = {"name": "ENGINE_SOCKET", "source": "explicit", "socket": socket}
    else:
        name = connection.get("Name")
        safe_connection = {"name": str(name) if name else "default", "source": "default"}
    return RuntimePreflight(
        state="ready_for_containment_experiment",
        code="ready",
        message="Rootless Podman setup is ready for the later containment experiment; containment is not verified.",
        runtime_tier=runtime_tier,
        engine_version=_version(info, "Version"),
        api_version=_version(info, "APIVersion"),
        connection=safe_connection,
        next_action="Run the T12 containment, proxy-only egress, and fresh-case conformance tests before dispatching agents.",
    )
