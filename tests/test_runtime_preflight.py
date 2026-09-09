"""Focused contract tests for the deliberately narrow T12a Podman preflight."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from llm_agent_eval.runtime.podman import preflight


Command = tuple[str, ...]


class FakePodman:
    """A subprocess seam with realistic Podman CLI payloads."""

    def __init__(self, responses: dict[Command, object]):
        self.responses = responses
        self.calls: list[Command] = []

    def __call__(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append(command)
        response = self.responses[command]
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(response), stderr="")


def rootless_info(*, version: str = "5.4.0") -> dict[str, object]:
    return {
        "host": {"security": {"rootless": True}},
        "version": {"Version": version, "APIVersion": "5.4.0"},
    }


def rootful_info() -> dict[str, object]:
    return {
        "host": {"security": {"rootless": False}},
        "version": {"Version": "5.4.0", "APIVersion": "5.4.0"},
    }


def default_connection(*, uri: str = "ssh://core@localhost:12345/run/user/501/podman/podman.sock") -> list[dict[str, object]]:
    return [{"Name": "podman-machine-default", "URI": uri, "Default": True}]


def test_missing_podman_returns_install_action() -> None:
    result = preflight(
        environment={},
        system="Darwin",
        command_runner=FakePodman({
            ("podman", "system", "connection", "list", "--format", "json"): FileNotFoundError(),
        }),
    )

    assert result.state == "blocked_setup"
    assert result.code == "runtime_unavailable"
    assert "Install Podman" in result.next_action


def test_rootful_engine_is_rejected_even_with_a_podman_connection() -> None:
    runner = FakePodman({
        ("podman", "system", "connection", "list", "--format", "json"): default_connection(),
        ("podman", "machine", "inspect"): {"Name": "podman-machine-default", "Running": True},
        ("podman", "info", "--format", "json"): rootful_info(),
    })

    result = preflight(environment={}, system="Darwin", command_runner=runner)

    assert result.code == "rootless_runtime_required"
    assert "rootless" in result.next_action.lower()


def test_macos_rootless_user_connection_is_ready_for_runc_with_observed_version() -> None:
    runner = FakePodman({
        ("podman", "system", "connection", "list", "--format", "json"): default_connection(),
        ("podman", "machine", "inspect"): {"Name": "podman-machine-default", "Running": True},
        ("podman", "info", "--format", "json"): rootless_info(version="5.5.1"),
    })

    result = preflight(environment={}, system="Darwin", command_runner=runner)

    assert result.state == "ready_for_containment_experiment"
    assert result.code == "ready"
    assert result.runtime_tier == "runc"
    assert result.engine_version == "5.5.1"
    assert result.connection == {"name": "podman-machine-default", "source": "default"}
    assert ("podman", "machine", "inspect") in runner.calls


def test_runsc_on_macos_is_a_classified_setup_blocker() -> None:
    result = preflight(environment={"AGENT_RUNTIME": "runsc"}, system="Darwin")

    assert result.state == "blocked_setup"
    assert result.code == "runtime_unsupported"
    assert "Linux" in result.next_action


def test_malformed_runtime_is_rejected_without_calling_an_engine() -> None:
    runner = FakePodman({})

    result = preflight(environment={"AGENT_RUNTIME": "docker"}, system="Linux", command_runner=runner)

    assert result.state == "blocked_setup"
    assert result.code == "runtime_invalid"
    assert runner.calls == []


def test_validated_socket_override_is_preserved_and_never_uses_docker() -> None:
    socket = "unix:///Users/example/.local/share/containers/podman.sock"
    runner = FakePodman({
        ("podman", "system", "connection", "list", "--format", "json"): default_connection(),
        ("podman", "machine", "inspect"): {"Name": "podman-machine-default", "Running": True},
        ("podman", "--url", socket, "info", "--format", "json"): rootless_info(),
    })

    result = preflight(
        environment={"ENGINE_SOCKET": socket}, system="Darwin", command_runner=runner
    )

    assert result.state == "ready_for_containment_experiment"
    assert result.connection == {
        "name": "ENGINE_SOCKET",
        "source": "explicit",
        "socket": socket,
    }
    assert ("podman", "--url", socket, "info", "--format", "json") in runner.calls
    assert all("docker" not in part.lower() for call in runner.calls for part in call)
