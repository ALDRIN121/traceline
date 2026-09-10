"""Contract tests for the deliberately narrow internal-network Podman probe."""

from __future__ import annotations

import re
import subprocess

import pytest

from llm_agent_eval.runtime.podman import (
    ContainmentProbeResult,
    RuntimePreflight,
    run_containment_probe,
)


Command = tuple[str, ...]
Response = int | BaseException
IMAGE = "docker.io/library/alpine:3.20"


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
            "httpd",
            "-f",
            "-p",
            "8080",
        ),
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
        "client_remove": (*prefix, "rm", "-f", client),
        "server_remove": (*prefix, "rm", "-f", server),
        "network_remove": (*prefix, "network", "rm", network),
    }


def runner_for(
    suffix: str,
    *,
    direct_returncode: int = 1,
    network_remove_returncode: int = 0,
    socket: str | None = None,
) -> FakePodman:
    probe_commands = commands(suffix, socket=socket)
    responses = {command: 0 for command in probe_commands.values()}
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
    assert runner.calls == list(commands("unit").values())
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
    assert runner.calls == list(probe_commands.values())


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
        probe_commands["network_remove"],
    ]


def test_probe_attempts_every_exact_cleanup_after_a_client_remove_failure() -> None:
    """Catches cleanup short-circuiting and leaking later exact resources."""
    probe_commands = commands("all-cleanup")
    responses = {command: 0 for command in probe_commands.values()}
    responses[probe_commands["direct_wget"]] = 1
    responses[probe_commands["client_remove"]] = 1
    runner = FakePodman(responses)

    result = run_containment_probe(
        ready_preflight(), command_runner=runner, probe_suffix="all-cleanup"
    )

    assert result.code == "cleanup_failed"
    assert result.cleanup == "failed"
    assert runner.calls == list(probe_commands.values())
