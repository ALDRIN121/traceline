from __future__ import annotations

import json
import subprocess

import pytest

from llm_agent_eval.runtime.build import BuildDenied, BuildService, RuntimeProfile


IMAGE = "sha256:" + "b" * 64


class FakePodman:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, timeout, env=None):
        self.calls.append((tuple(args), timeout))
        if args[1] == "build":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": IMAGE}]), "")
        raise AssertionError(args)


def test_restricted_build_returns_digest_and_never_inherits_environment(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    runner = FakePodman()
    service = BuildService(command_runner=runner, environment={"PATH": "/bin"})

    job = service.prepare(
        source,
        RuntimeProfile(base_image=IMAGE, entrypoint=("/usr/bin/python", "/source/agent.py")),
    )

    assert job.state == "prepared"
    assert job.image_digest == IMAGE
    build = runner.calls[0][0]
    assert build[:2] == ("podman", "build")
    assert "--network=none" in build
    assert "--pull=never" in build
    assert "--cap-drop=ALL" in build
    assert "--security-opt=no-new-privileges" in build
    assert not any("API" in part or "SECRET" in part for part in build)


def test_build_rejects_mutable_base_images_before_podman(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    runner = FakePodman()
    service = BuildService(command_runner=runner)

    with pytest.raises(BuildDenied):
        service.prepare(source, RuntimeProfile(base_image="python:3.12", entrypoint=("/bin/sh",)))
    assert runner.calls == []


def test_build_rejects_symlinked_context_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to("/etc/passwd")

    with pytest.raises(BuildDenied):
        BuildService().prepare(
            source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",))
        )


def test_podman_raw_image_id_is_normalized_to_digest():
    assert BuildService._parse_image_digest("a" * 64) == "sha256:" + "a" * 64
