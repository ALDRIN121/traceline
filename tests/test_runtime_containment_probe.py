"""Contract tests for the deliberately narrow internal-network Podman probe."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from llm_agent_eval.runtime.podman import (
    ContainmentProbeResult,
    RuntimePreflight,
    run_containment_probe,
)


Command = tuple[str, ...]
Response = int | BaseException
IMAGE = "docker.io/library/alpine:3.20"
WITNESS_SCRIPT = (
    'while true; do printf "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n'
    'Connection: close\\r\\n\\r\\nok" | nc -l -p 8080; done'
)


def test_startup_script_never_selects_docker_or_local_execution_fallback() -> None:
    """Catches the unsafe startup branches that bypass containment setup."""
    script = (Path(__file__).resolve().parents[1] / "scripts/spin_up.sh").read_text(
        encoding="utf-8"
    )

    assert "docker compose" not in script
    assert "docker info" not in script
    assert "Falling back to local live execution" not in script
    assert "runtime_preflight.py" in script


class FakePodman:
    """A deterministic subprocess seam that records complete Podman vectors."""

    def __init__(self, responses: dict[Command, Response]):
        self.responses = responses
        self.calls: list[Command] = []

    def __call__(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append(command)
        response = self.responses[command]
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(args, response, stdout="", stderr="")


class SuccessfulPodman:
    """A generic successful lifecycle seam, except for the required direct dial."""

    def __init__(self) -> None:
        self.calls: list[Command] = []

    def __call__(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append(command)
        direct_dial = "http://203.0.113.1:81" in command
        return subprocess.CompletedProcess(args, 1 if direct_dial else 0, stdout="", stderr="")


def ready_preflight(*, explicit_socket: str | None = None) -> RuntimePreflight:
    connection = {"name": "default", "source": "default"}
    if explicit_socket is not None:
        connection = {
            "name": "ENGINE_SOCKET",
            "source": "explicit",
            "socket": explicit_socket,
        }
    return RuntimePreflight(
        state="ready_for_containment_experiment",
        code="ready",
        message="ready",
        runtime_tier="runc",
        next_action="probe",
        engine_version="5.5.1",
        connection=connection,
    )


def blocked_preflight() -> RuntimePreflight:
    return RuntimePreflight(
        state="blocked_setup",
        code="runtime_unavailable",
        message="blocked",
        runtime_tier="runc",
        next_action="install",
    )


def commands(suffix: str, *, socket: str | None = None) -> dict[str, Command]:
    network = f"llm-agent-eval-containment-{suffix}"
    server = f"{network}-server"
    client = f"{network}-client"
    prefix = ("podman",) if socket is None else ("podman", "--url", socket)
    return {
        "network_create": (*prefix, "network", "create", "--internal", network),
        "network_exists": (*prefix, "network", "exists", network),
        "server_run": (
            *prefix,
            "run",
            "--detach",
            "--name",
            server,
            "--network",
            network,
            "--network-alias",
            "probe",
            IMAGE,
            "/bin/sh",
            "-c",
            WITNESS_SCRIPT,
        ),
        "server_exists": (*prefix, "container", "exists", server),
        "client_exists": (*prefix, "container", "exists", client),
        "client_run": (
            *prefix,
            "run",
            "--detach",
            "--name",
            client,
            "--network",
            network,
            IMAGE,
            "sleep",
            "60",
        ),
        "in_network_wget": (
            *prefix,
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
        "direct_wget": (
            *prefix,
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
        "client_remove": (*prefix, "rm", "-f", "--time", "0", client),
        "server_remove": (*prefix, "rm", "-f", "--time", "0", server),
        "network_remove": (*prefix, "network", "rm", network),
    }


def previous_server_run(suffix: str, *, socket: str | None = None) -> Command:
    """Represent the unavailable BusyBox witness vector before its replacement."""
    server_run = commands(suffix, socket=socket)["server_run"]
    shell_index = server_run.index("/bin/sh")
    return (*server_run[:shell_index], "busybox", "httpd", "-f", "-p", "8080")


def successful_call_order(suffix: str, *, socket: str | None = None) -> list[Command]:
    probe_commands = commands(suffix, socket=socket)
    return [
        probe_commands[key]
        for key in (
            "network_create",
            "server_run",
            "client_run",
            "in_network_wget",
            "direct_wget",
            "client_remove",
            "server_remove",
            "network_remove",
        )
    ]


def runner_for(
    suffix: str,
    *,
    direct_returncode: int = 1,
    network_remove_returncode: int = 0,
    socket: str | None = None,
) -> FakePodman:
    probe_commands = commands(suffix, socket=socket)
    responses = {command: 0 for command in probe_commands.values()}
    responses[previous_server_run(suffix, socket=socket)] = 0
    responses[probe_commands["direct_wget"]] = direct_returncode
    responses[probe_commands["network_remove"]] = network_remove_returncode
    return FakePodman(responses)


def test_probe_refuses_resources_when_preflight_is_not_ready() -> None:
    """Catches removal of the preflight gate before any Podman lifecycle call."""
    runner = FakePodman({})

    result = run_containment_probe(
        blocked_preflight(), command_runner=runner, probe_suffix="unit"
    )

    assert result.state == "blocked_setup"
    assert result.network is None
    assert runner.calls == []


def test_probe_observes_internal_http_and_blocked_direct_egress() -> None:
    """Catches an unsafe topology, unbounded command, or missing exact cleanup."""
    runner = runner_for("unit")

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="unit"
    )

    assert isinstance(result, ContainmentProbeResult)
    assert result.state == "containment_probe_observed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "blocked"
    assert result.cleanup == "complete"
    assert runner.calls == successful_call_order("unit")
    forbidden = {"docker", "docker-compose", "--volume", "--mount", "ENGINE_SOCKET"}
    assert all(call[0] == "podman" for call in runner.calls)
    assert all(not (forbidden & set(call)) for call in runner.calls)
    assert all("api-key" not in part.lower() for call in runner.calls for part in call)


def test_probe_uses_the_preflight_validated_explicit_socket_for_every_command() -> None:
    """Catches a lifecycle command that switches engines after preflight."""
    socket = "unix:///Users/example/.local/share/containers/podman.sock"
    runner = runner_for("socket", socket=socket)

    result = run_containment_probe(
        ready_preflight(explicit_socket=socket), command_runner=runner, probe_suffix="socket"
    )

    assert result.state == "containment_probe_observed"
    assert all(call[:3] == ("podman", "--url", socket) for call in runner.calls)


def test_probe_starts_witness_with_fixed_alpine_shell_nc_server() -> None:
    """Catches a witness vector that is not the validated static shell/``nc`` server."""
    runner = SuccessfulPodman()

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="busybox"
    )

    assert result.state == "containment_probe_observed"
    assert commands("busybox")["server_run"] in runner.calls


def test_probe_forces_exact_container_cleanup_without_a_stop_grace_period() -> None:
    """Catches Podman's default stop grace period racing the command timeout."""
    runner = SuccessfulPodman()

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="immediate-cleanup"
    )

    assert result.cleanup == "complete"
    assert runner.calls[-3:] == [
        (
            "podman", "rm", "-f", "--time", "0",
            "llm-agent-eval-containment-immediate-cleanup-client",
        ),
        (
            "podman", "rm", "-f", "--time", "0",
            "llm-agent-eval-containment-immediate-cleanup-server",
        ),
        ("podman", "network", "rm", "llm-agent-eval-containment-immediate-cleanup"),
    ]


def test_probe_generates_an_opaque_name_when_no_suffix_is_supplied() -> None:
    """Catches resource-name reuse when callers omit the test-only suffix."""
    runner = SuccessfulPodman()

    result = run_containment_probe(ready_preflight(), command_runner=runner)

    assert result.network is not None
    assert re.fullmatch(r"llm-agent-eval-containment-[0-9a-f]{24}", result.network)
    assert all(
        any(result.network in argument for argument in call) for call in runner.calls
    )


def test_probe_marks_reachable_direct_egress_as_security_failure() -> None:
    """Catches treating a successful direct outbound dial as a normal observation."""
    runner = runner_for("direct", direct_returncode=0)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="direct"
    )

    assert result.code == "egress_boundary_failed"
    assert result.direct_egress == "reachable"


@pytest.mark.parametrize(
    "direct_failure",
    [
        FileNotFoundError(),
        OSError("Podman connection failed"),
        subprocess.TimeoutExpired(["podman", "exec"], 10),
    ],
    ids=["missing_podman", "runner_error", "timeout"],
)
def test_probe_does_not_treat_direct_probe_execution_failure_as_blocked(
    direct_failure: BaseException,
) -> None:
    """Catches exceptions being collapsed into a false direct-egress result."""
    probe_commands = commands("direct-error")
    responses: dict[Command, Response] = {command: 0 for command in probe_commands.values()}
    responses[previous_server_run("direct-error")] = 0
    responses[probe_commands["direct_wget"]] = direct_failure
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="direct-error"
    )

    assert result.state == "containment_probe_failed"
    assert result.code == "containment_probe_failed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "not_observed"
    assert result.cleanup == "complete"
    assert runner.calls == successful_call_order("direct-error")


@pytest.mark.parametrize("returncode", [2, 125, 126, 127, 137, 143, -9, -15])
def test_probe_rejects_unexpected_direct_exec_status(returncode: int) -> None:
    """Catches engine, launch, signal, and other errors becoming denial evidence."""
    runner = runner_for("unexpected-status", direct_returncode=returncode)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="unexpected-status"
    )

    assert result.state == "containment_probe_failed"
    assert result.code == "containment_probe_failed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "not_observed"
    assert result.cleanup == "complete"
    assert runner.calls == successful_call_order("unexpected-status")


@pytest.mark.parametrize(
    ("creation", "existence", "creation_order", "prior_cleanup"),
    [
        ("network_create", "network_exists", ["network_create"], []),
        ("server_run", "server_exists", ["network_create", "server_run"], ["network_remove"]),
        (
            "client_run", "client_exists",
            ["network_create", "server_run", "client_run"],
            ["server_remove", "network_remove"],
        ),
    ],
)
@pytest.mark.parametrize(
    "creation_failure",
    [1, 125, subprocess.TimeoutExpired(["podman"], 10), OSError("connection lost")],
    ids=["nonzero", "engine-error", "timeout", "runner-error"],
)
@pytest.mark.parametrize("exists_status", [0, 1], ids=["exists", "absent"])
def test_probe_reconciles_every_uncertain_creation_by_exact_name(
    creation: str,
    existence: str,
    creation_order: list[str],
    prior_cleanup: list[str],
    creation_failure: Response,
    exists_status: int,
) -> None:
    """Catches orphaned resources or continued probing after an uncertain create."""
    socket = "unix:///Users/example/.local/share/containers/podman.sock"
    probe_commands = commands("uncertain-create", socket=socket)
    responses: dict[Command, Response] = {command: 0 for command in probe_commands.values()}
    responses[probe_commands[creation]] = creation_failure
    responses[probe_commands[existence]] = exists_status
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(explicit_socket=socket), command_runner=runner,
        probe_suffix="uncertain-create",
    )

    assert result.code == "containment_probe_failed"
    assert result.cleanup == "complete"
    assert result.in_network_http == result.direct_egress == "not_observed"
    removal = existence.replace("_exists", "_remove")
    expected_cleanup = ([removal] if exists_status == 0 else []) + prior_cleanup
    assert runner.calls == [
        probe_commands[key] for key in [*creation_order, existence, *expected_cleanup]
    ]


@pytest.mark.parametrize(
    ("creation", "existence", "creation_order", "prior_cleanup"),
    [
        ("network_create", "network_exists", ["network_create"], []),
        ("server_run", "server_exists", ["network_create", "server_run"], ["network_remove"]),
        (
            "client_run", "client_exists",
            ["network_create", "server_run", "client_run"],
            ["server_remove", "network_remove"],
        ),
    ],
)
@pytest.mark.parametrize(
    "existence_failure",
    [2, 125, subprocess.TimeoutExpired(["podman"], 10), OSError("connection lost")],
    ids=["unexpected-status", "engine-error", "timeout", "runner-error"],
)
def test_probe_never_claims_complete_cleanup_when_exact_existence_is_unknown(
    creation: str,
    existence: str,
    creation_order: list[str],
    prior_cleanup: list[str],
    existence_failure: Response,
) -> None:
    """Catches uncertain existence being mistaken for absence or stopping cleanup."""
    probe_commands = commands("unknown-existence")
    responses: dict[Command, Response] = {command: 0 for command in probe_commands.values()}
    responses[probe_commands[creation]] = subprocess.TimeoutExpired(["podman"], 10)
    responses[probe_commands[existence]] = existence_failure
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="unknown-existence"
    )

    assert result.state == "containment_probe_failed"
    assert result.code == "cleanup_failed"
    assert result.cleanup == "failed"
    assert result.in_network_http == result.direct_egress == "not_observed"
    assert runner.calls == [
        probe_commands[key] for key in [*creation_order, existence, *prior_cleanup]
    ]


def test_probe_reports_cleanup_failure_without_losing_boundary_observations() -> None:
    """Catches cleanup errors that erase an otherwise useful boundary observation."""
    runner = runner_for("cleanup", network_remove_returncode=1)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="cleanup"
    )

    assert result.code == "cleanup_failed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "blocked"


def test_probe_classifies_cleanup_failure_after_a_partial_lifecycle_failure() -> None:
    """Catches a return-before-finally result that hides failed exact cleanup."""
    probe_commands = commands("partial")
    responses = {command: 0 for command in probe_commands.values()}
    responses[probe_commands["server_run"]] = 1
    responses[previous_server_run("partial")] = 1
    responses[probe_commands["network_remove"]] = 1
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="partial"
    )

    assert result.code == "cleanup_failed"
    assert result.cleanup == "failed"
    assert result.in_network_http == "not_observed"
    assert result.direct_egress == "not_observed"
    assert runner.calls == [
        probe_commands["network_create"],
        probe_commands["server_run"],
        probe_commands["server_exists"],
        probe_commands["server_remove"],
        probe_commands["network_remove"],
    ]


def test_probe_attempts_every_exact_cleanup_after_a_client_remove_failure() -> None:
    """Catches cleanup short-circuiting and leaking later exact resources."""
    probe_commands = commands("all-cleanup")
    responses = {command: 0 for command in probe_commands.values()}
    responses[previous_server_run("all-cleanup")] = 0
    responses[probe_commands["direct_wget"]] = 1
    responses[probe_commands["client_remove"]] = 1
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="all-cleanup"
    )

    assert result.code == "cleanup_failed"
    assert result.cleanup == "failed"
    assert runner.calls == successful_call_order("all-cleanup")


def test_probe_removes_a_named_server_that_exists_after_failed_start() -> None:
    """Catches leaking a named partial server after its run command exits nonzero."""
    probe_commands = commands("partial-server")
    responses: dict[Command, Response] = {command: 0 for command in probe_commands.values()}
    responses[probe_commands["server_run"]] = 1
    responses[previous_server_run("partial-server")] = 1
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="partial-server"
    )

    assert result.code == "containment_probe_failed"
    assert result.cleanup == "complete"
    assert runner.calls == [
        probe_commands["network_create"],
        probe_commands["server_run"],
        probe_commands["server_exists"],
        probe_commands["server_remove"],
        probe_commands["network_remove"],
    ]


def test_probe_does_not_remove_an_absent_server_after_failed_start() -> None:
    """Catches a false cleanup target when a named server never materialized."""
    probe_commands = commands("absent-server")
    responses: dict[Command, Response] = {command: 0 for command in probe_commands.values()}
    responses[probe_commands["server_run"]] = 1
    responses[probe_commands["server_exists"]] = 1
    responses[previous_server_run("absent-server")] = 1
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="absent-server"
    )

    assert result.code == "containment_probe_failed"
    assert result.cleanup == "complete"
    assert runner.calls == [
        probe_commands["network_create"],
        probe_commands["server_run"],
        probe_commands["server_exists"],
        probe_commands["network_remove"],
    ]
