"""Per-case sandbox policy tests use synthetic local fixtures only."""

import io
from pathlib import Path
import subprocess
import tarfile

import pytest

import llm_agent_eval.runtime.sandbox as sandbox_module
from llm_agent_eval.runtime.sandbox import (
    SandboxDenied, SandboxLimits, SandboxRequest, PodmanSandbox,
    snapshot_digest, read_output_archive,
)

IMAGE = "sha256:" + "a" * 64


def request(tmp_path, **changes):
    source, inputs = tmp_path / "source", tmp_path / "inputs"
    source.mkdir(exist_ok=True)
    inputs.mkdir(exist_ok=True)
    (source / "agent.sh").write_text("printf ok")
    (inputs / "case.json").write_text('{"input":"hello"}')
    values = dict(image=IMAGE, source_dir=source, source_digest=snapshot_digest(source),
                  input_dir=inputs, input_digest=snapshot_digest(inputs),
                  argv=("/bin/sh", "/source/agent.sh"))
    return SandboxRequest(**(values | changes))


@pytest.mark.parametrize("changes", [dict(cpus=0), dict(memory_mb=0), dict(pids=0),
    dict(timeout_seconds=0), dict(output_mb=0), dict(cpus=float("nan")),
    dict(timeout_seconds=float("inf"))])
def test_invalid_limits(changes):
    with pytest.raises(SandboxDenied):
        SandboxLimits(**changes)


def test_mutable_image_and_unconfigured_egress_rejected(tmp_path):
    with pytest.raises(SandboxDenied):
        request(tmp_path, image="alpine:latest")
    with pytest.raises(SandboxDenied):
        request(tmp_path, egress="host")


def test_mounts_and_no_environment_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_INSTALL_SECRET", "not-for-agent")
    req = request(tmp_path)
    sandbox = PodmanSandbox()
    args = sandbox.run_argument_vector(req, "case-fixed", req.source_dir, req.input_dir)
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--pull=never" in args
    assert "--user=65532:65532" in args
    assert "--memory=512m" in args
    assert "--memory-swap=512m" in args
    assert "--pids-limit=64" in args
    assert "--cpus=1.0" in args
    mounts = [x for x in args if x.startswith("type=")]
    assert len(mounts) == 2
    assert all("readonly" in x for x in mounts)
    assert not any("target=/output" in x for x in mounts)
    assert any(x.startswith("/output:rw,size=67108864") for x in args)
    assert not any("SYNTHETIC_INSTALL_SECRET" in x for x in args)
    assert not any("podman.sock" in x or "docker.sock" in x for x in args)


def test_proxy_egress_requires_engine_owned_network_and_ca_and_injects_only_proxy_env(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CA")
    req = request(
        tmp_path, egress="proxy", proxy_endpoint="http://proxy:8080",
        network_name="run-network", network_run_id="run-1", ca_cert=ca,
    )
    args = PodmanSandbox().run_argument_vector(req, "case-fixed", req.source_dir, req.input_dir)
    assert "--network=run-network" in args
    assert "--env=HTTP_PROXY=http://proxy:8080" in args
    assert "--env=HTTPS_PROXY=http://proxy:8080" in args
    assert "--env=NO_PROXY=" in args
    assert any("target=/run/llm-agent-eval/ca.pem" in x and "readonly" in x for x in args)


def test_proxy_egress_without_network_or_ca_is_rejected(tmp_path):
    with pytest.raises(SandboxDenied):
        request(tmp_path, egress="proxy", proxy_endpoint="http://proxy:8080")


def test_changed_source_and_symlinks_rejected(tmp_path):
    req = request(tmp_path)
    (req.source_dir / "agent.sh").write_text("changed")
    with pytest.raises(SandboxDenied, match="digest"):
        PodmanSandbox().validate_snapshot(req.source_dir, req.source_digest)
    (req.source_dir / "link").symlink_to(req.input_dir / "case.json")
    with pytest.raises(SandboxDenied):
        snapshot_digest(req.source_dir)


def test_snapshot_digest_does_not_require_posix_no_follow_flag(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.sh").write_text("printf ok")
    monkeypatch.setattr(sandbox_module, "_snapshot_open_flags", lambda: sandbox_module.os.O_RDONLY)

    digest = snapshot_digest(source)

    assert digest["files"] == {"agent.sh": digest["files"]["agent.sh"]}


def test_output_tar_never_extracts_paths(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("result.json")
        content = b'{"ok":true}'
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    assert read_output_archive(stream.getvalue(), 1024) == {"result.json": b'{"ok":true}'}


@pytest.mark.parametrize("name,kind", [("../secret", tarfile.REGTYPE),
    ("/secret", tarfile.REGTYPE), ("link", tarfile.SYMTYPE),
    ("fifo", tarfile.FIFOTYPE)])
def test_untrusted_output_archive_rejected(name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/synthetic-secret"
        archive.addfile(info)
    with pytest.raises(SandboxDenied):
        read_output_archive(stream.getvalue(), 1024)


def test_missing_podman_fails_closed(tmp_path, monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(subprocess, "Popen", absent)
    result = PodmanSandbox().run(request(tmp_path))
    assert result.state == "blocked_setup"
    assert result.code == "runtime_unavailable"
    assert result.cleanup == "not_started"
