from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_agent_eval.runtime.build import BuildDenied, BuildService, RuntimeProfile


IMAGE = "sha256:" + "b" * 64


class FakePodman:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, timeout, env=None):
        self.calls.append((tuple(args), timeout))
        if args[-3:] == ["info", "--format", "json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"host": {"security": {"rootless": True}}}), "",
            )
        if args[1] == "build":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": IMAGE}]), "")
        raise AssertionError(args)


class ConfiguredFakePodman:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, timeout, env=None):
        self.calls.append((tuple(args), timeout, dict(env or {})))
        if args[-3:] == ["info", "--format", "json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"host": {"security": {"rootless": True}}}), "",
            )
        if "build" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "image" in args and "inspect" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Id": IMAGE}]), "")
        raise AssertionError(args)


class RootfulFakePodman:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, timeout, env=None):
        self.calls.append(tuple(args))
        if args[-3:] == ["info", "--format", "json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"host": {"security": {"rootless": False}}}), "",
            )
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
    build = next(call[0] for call in runner.calls if "build" in call[0])
    assert build[:2] == ("podman", "build")
    assert "--network=none" in build
    assert "--pull=never" in build
    assert "--cap-drop=ALL" in build
    assert "--security-opt=no-new-privileges" in build
    assert not any("API" in part or "SECRET" in part for part in build)
    assert job.provenance["adapter_version"] == "local-target-v1"
    assert job.provenance["lockfile_digest"] == "none"
    assert job.provenance["cache_key"]
    assert any("llm-agent-eval.cache-key=" in part for part in build)


def test_build_cache_key_changes_for_lockfile_base_policy_and_adapter(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    (source / "uv.lock").write_text("version = 1\n")

    first = BuildService(command_runner=FakePodman(), adapter_version="local-target-v1")
    first_job = first.prepare(
        source,
        RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",)),
    )

    (source / "uv.lock").write_text("version = 2\n")
    changed_lockfile = BuildService(
        command_runner=FakePodman(), adapter_version="local-target-v1"
    ).prepare(source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",)))
    changed_base = BuildService(
        command_runner=FakePodman(), adapter_version="local-target-v1"
    ).prepare(
        source,
        RuntimeProfile(base_image="sha256:" + "c" * 64, entrypoint=("/bin/sh",)),
    )
    changed_policy = BuildService(
        command_runner=FakePodman(), adapter_version="local-target-v1"
    ).prepare(
        source,
        RuntimeProfile(
            base_image=IMAGE, entrypoint=("/bin/sh",), policy_version="policy-v2"
        ),
    )
    changed_adapter = BuildService(
        command_runner=FakePodman(), adapter_version="local-target-v2"
    ).prepare(source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",)))

    assert changed_lockfile.provenance["lockfile_digest"] != first_job.provenance["lockfile_digest"]
    assert changed_lockfile.provenance["cache_key"] != first_job.provenance["cache_key"]
    assert changed_base.provenance["cache_key"] != changed_lockfile.provenance["cache_key"]
    assert changed_policy.provenance["cache_key"] != changed_lockfile.provenance["cache_key"]
    assert changed_adapter.provenance["cache_key"] != changed_lockfile.provenance["cache_key"]


def test_restricted_build_uses_the_explicit_rootless_engine_socket(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    runner = ConfiguredFakePodman()
    service = BuildService(
        command_runner=runner,
        environment={
            "PATH": "/bin",
            "HOME": str(tmp_path),
            "ENGINE_SOCKET": "unix:///tmp/rootless-podman.sock",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
    )

    service.prepare(
        source,
        RuntimeProfile(base_image=IMAGE, entrypoint=("/usr/bin/python", "/source/agent.py")),
    )

    build = next(call[0] for call in runner.calls if "build" in call[0])
    assert build[:3] == ("podman", "--url", "unix:///tmp/rootless-podman.sock")
    assert "build" in build
    build_call = next(call for call in runner.calls if "build" in call[0])
    assert build_call[2] == {
        "PATH": "/bin", "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }


def test_restricted_build_rejects_a_rootful_engine_before_building(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    runner = RootfulFakePodman()

    with pytest.raises(BuildDenied, match="rootless"):
        BuildService(command_runner=runner).prepare(
            source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",))
        )
    assert not any("build" in call for call in runner.calls)


def test_build_rejects_mutable_base_images_before_podman(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    runner = FakePodman()
    service = BuildService(command_runner=runner)

    with pytest.raises(BuildDenied):
        service.prepare(source, RuntimeProfile(base_image="python:3.12", entrypoint=("/bin/sh",)))
    assert not any("build" in call[0] for call in runner.calls)


def test_build_rejects_symlinked_context_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to("/etc/passwd")

    with pytest.raises(BuildDenied):
        BuildService().prepare(
            source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",))
        )


def test_restricted_build_rejects_a_staged_tree_that_does_not_match_its_digest(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")

    import llm_agent_eval.runtime.build as build_module

    original_copytree = build_module.shutil.copytree

    def tampering_copytree(src, dst, **kwargs):
        result = original_copytree(src, dst, **kwargs)
        Path(dst, "agent.py").write_text("print('tampered')")
        return result

    monkeypatch.setattr(build_module.shutil, "copytree", tampering_copytree)
    runner = FakePodman()

    with pytest.raises(BuildDenied, match="staged source snapshot"):
        BuildService(command_runner=runner).prepare(
            source, RuntimeProfile(base_image=IMAGE, entrypoint=("/bin/sh",))
        )
    assert not any("build" in call[0] for call in runner.calls)


def test_podman_raw_image_id_is_normalized_to_digest():
    assert BuildService._parse_image_digest("a" * 64) == "sha256:" + "a" * 64
