"""Rootless Podman preflight and a deliberately narrow containment probe.

The probe creates an internal witness topology only.  It does not configure a
proxy, trust a CA, broker credentials, or certify the later egress boundary.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


PODMAN_TIMEOUT_SECONDS = 10.0
SUPPORTED_RUNTIMES = frozenset({"runc", "runsc", "vm"})
PROBE_IMAGE = "docker.io/library/alpine:3.20"
_PROBE_SUFFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_WITNESS_SCRIPT = (
    'while true; do printf "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n'
    'Connection: close\\r\\n\\r\\nok" | nc -l -p 8080; done'
)

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


@dataclass(frozen=True)
class ContainmentProbeResult:
    """Public observations from the deliberately limited internal-network probe."""

    state: str
    code: str
    message: str
    network: str | None
    in_network_http: str
    direct_egress: str
    cleanup: str
    engine_version: str | None


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


def _probe_prefix(preflight_result: RuntimePreflight) -> list[str] | None:
    """Reuse only the preflight's explicit, already-rootless socket selection."""
    connection = preflight_result.connection
    if not connection or connection.get("source") != "explicit":
        return ["podman"]
    socket = connection.get("socket")
    if not isinstance(socket, str) or _socket_override(socket) != socket or _is_root_connection(socket):
        return None
    return ["podman", "--url", socket]


def _probe_command(prefix: list[str], *arguments: str) -> list[str]:
    """Make a new fixed vector without accepting shell syntax or arbitrary options."""
    return [*prefix, *arguments]


def _probe_returncode(command: list[str], runner: CommandRunner) -> int | None:
    """Return an observed exit status; execution failures have no observation."""
    try:
        return runner(command, timeout=PODMAN_TIMEOUT_SECONDS).returncode
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def _probe_succeeded(command: list[str], runner: CommandRunner) -> bool:
    return _probe_returncode(command, runner) == 0


def run_containment_probe(
    preflight_result: RuntimePreflight,
    *,
    command_runner: CommandRunner = _run_podman,
    probe_suffix: str | None = None,
) -> ContainmentProbeResult:
    """Observe one short-lived internal Podman topology without fallback behavior.

    This is intentionally only a network topology probe.  It neither configures
    a proxy nor certifies any of the later proxy, TLS, or credential boundaries.
    """
    if preflight_result.state != "ready_for_containment_experiment":
        return ContainmentProbeResult(
            state="blocked_setup",
            code="preflight_not_ready",
            message="Rootless Podman preflight must be ready before the containment probe runs.",
            network=None,
            in_network_http="not_observed",
            direct_egress="not_observed",
            cleanup="not_started",
            engine_version=preflight_result.engine_version,
        )

    prefix = _probe_prefix(preflight_result)
    if prefix is None:
        return ContainmentProbeResult(
            state="blocked_setup",
            code="preflight_connection_invalid",
            message="The preflight connection is not a safe rootless Podman socket.",
            network=None,
            in_network_http="not_observed",
            direct_egress="not_observed",
            cleanup="not_started",
            engine_version=preflight_result.engine_version,
        )

    suffix = probe_suffix if probe_suffix is not None else secrets.token_hex(12)
    if not _PROBE_SUFFIX.fullmatch(suffix):
        return ContainmentProbeResult(
            state="blocked_setup",
            code="probe_suffix_invalid",
            message="The containment probe suffix must be an opaque lowercase resource label.",
            network=None,
            in_network_http="not_observed",
            direct_egress="not_observed",
            cleanup="not_started",
            engine_version=preflight_result.engine_version,
        )

    network = f"llm-agent-eval-containment-{suffix}"
    server = f"{network}-server"
    client = f"{network}-client"
    resource_attempts: list[tuple[str, str, bool]] = []
    network_started = False
    server_started = False
    client_started = False
    in_network_http = "not_observed"
    direct_egress = "not_observed"
    state = "containment_probe_failed"
    code = "containment_probe_failed"
    message = "The containment probe could not complete its fixed topology observations."
    cleanup = "complete"

    try:
        network_started = _probe_succeeded(
            _probe_command(prefix, "network", "create", "--internal", network), command_runner
        )
        resource_attempts.append(("network", network, network_started))
        if network_started:
            server_started = _probe_succeeded(
                _probe_command(
                    prefix,
                    "run",
                    "--detach",
                    "--name",
                    server,
                    "--network",
                    network,
                    "--network-alias",
                    "probe",
                    PROBE_IMAGE,
                    "/bin/sh",
                    "-c",
                    _WITNESS_SCRIPT,
                ),
                command_runner,
            )
            resource_attempts.append(("container", server, server_started))
        if server_started:
            client_started = _probe_succeeded(
                _probe_command(
                    prefix,
                    "run",
                    "--detach",
                    "--name",
                    client,
                    "--network",
                    network,
                    PROBE_IMAGE,
                    "sleep",
                    "60",
                ),
                command_runner,
            )
            resource_attempts.append(("container", client, client_started))
        if client_started and _probe_succeeded(
            _probe_command(
                prefix,
                "exec",
                client,
                "wget",
                "-q",
                "-T",
                "3",
                "-O",
                "-",
                "http://probe:8080",
            ),
            command_runner,
        ):
            in_network_http = "reachable"
            direct_returncode = _probe_returncode(
                _probe_command(
                    prefix,
                    "exec",
                    client,
                    "wget",
                    "-q",
                    "-T",
                    "3",
                    "-O",
                    "-",
                    "http://203.0.113.1:81",
                ),
                command_runner,
            )
            if direct_returncode == 0:
                direct_egress = "reachable"
                state = "containment_probe_failed"
                code = "egress_boundary_failed"
                message = "The internal-network client unexpectedly reached direct egress."
            elif direct_returncode == 1:
                # Only the validated wget denial status is evidence of a dial.
                # Engine, launch, signal, and unexpected exits prove no boundary.
                direct_egress = "blocked"
                state = "containment_probe_observed"
                code = "containment_observed"
                message = "The internal network reached its witness and blocked the direct egress dial."
    finally:
        cleanup_failed = False
        for resource_type, name, started in reversed(resource_attempts):
            if not started:
                # A failed/timed-out create can still leave its exact named resource.
                # Existence errors must never be collapsed into confirmed absence.
                exists_returncode = _probe_returncode(
                    _probe_command(prefix, resource_type, "exists", name), command_runner
                )
                if exists_returncode == 1:
                    continue
                if exists_returncode != 0:
                    cleanup_failed = True
                    continue
            command = (
                _probe_command(prefix, "network", "rm", name)
                if resource_type == "network"
                else _probe_command(prefix, "rm", "-f", "--time", "0", name)
            )
            if not _probe_succeeded(command, command_runner):
                cleanup_failed = True
        if cleanup_failed:
            cleanup = "failed"
            state = "containment_probe_failed"
            code = "cleanup_failed"
            message = "The containment probe cleanup did not complete."

    return ContainmentProbeResult(
        state=state,
        code=code,
        message=message,
        network=network,
        in_network_http=in_network_http,
        direct_egress=direct_egress,
        cleanup=cleanup,
        engine_version=preflight_result.engine_version,
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
        or bool(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
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

    connection: dict[str, Any] | None = None
    if not socket:
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
        if error == "missing":
            return _blocked(
                "runtime_unavailable",
                "Podman is not installed or is not available on PATH.",
                runtime_tier,
                "Install Podman and configure a rootless connection before continuing.",
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
    if error == "missing":
        return _blocked(
            "runtime_unavailable",
            "Podman is not installed or is not available on PATH.",
            runtime_tier,
            "Install Podman and configure a rootless connection before continuing.",
        )
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
        assert connection is not None
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
