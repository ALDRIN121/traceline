"""Rootless, network-disabled, fresh-per-case Podman execution.

Inputs must be sanitized immutable snapshots prepared by the import pipeline.
Digests here check byte identity, not sanitization or source authorization.
Only engine-owned staging copies are mounted; output is a bounded tmpfs and
is read as data while the container is alive, never extracted onto the host.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .podman import _is_root_connection, _socket_override

MAX_TREE_FILES = 4096
MAX_TREE_BYTES = 64 * 1024 * 1024
STREAM_CAP_BYTES = 1024 * 1024
COMMAND_TIMEOUT = 10.0


class SandboxDenied(ValueError):
    """A request or runtime cannot meet the containment contract."""


@dataclass(frozen=True)
class SandboxLimits:
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 64
    timeout_seconds: float = 60.0
    output_mb: int = 64

    def __post_init__(self):
        for name, low, high, integer in (
            ("memory_mb", 32, 8192, True), ("cpus", .1, 8, False),
            ("pids", 8, 512, True), ("timeout_seconds", 1, 3600, False),
            ("output_mb", 1, 512, True),
        ):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high
                    or (integer and not isinstance(value, int))):
                raise SandboxDenied(f"invalid {name}")


def snapshot_digest(root: Path) -> dict:
    """Bounded regular-file tree digest; no symlinks, hardlinks or devices."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise SandboxDenied("snapshot must be a directory")
    files, total, count = {}, 0, 0
    for path in sorted(root.rglob("*")):
        count += 1
        if count > MAX_TREE_FILES:
            raise SandboxDenied("snapshot file count limit")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxDenied("snapshot contains a link or special file")
        total += info.st_size
        if total > MAX_TREE_BYTES:
            raise SandboxDenied("snapshot byte limit")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_TREE_BYTES + 1)
        if len(data) != info.st_size:
            raise SandboxDenied("snapshot changed during read")
        files[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    if not files:
        raise SandboxDenied("empty snapshot")
    return {"algorithm": "sha256", "files": files}


def read_output_archive(archive_bytes: bytes, max_bytes: int) -> dict[str, bytes]:
    """Parse bounded tar data without filesystem extraction or link following."""
    if len(archive_bytes) > max_bytes + MAX_TREE_FILES * 1024 + 10240:
        raise SandboxDenied("output archive byte limit")
    files, total, count = {}, 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            for member in archive:
                count += 1
                path = PurePosixPath(member.name)
                if (count > MAX_TREE_FILES or path.is_absolute() or ".." in path.parts
                        or "\\" in member.name or "\x00" in member.name):
                    raise SandboxDenied("unsafe output archive path")
                if member.isdir():
                    continue
                if not member.isfile() or not path.parts:
                    raise SandboxDenied("output contains a non-regular entry")
                name = path.as_posix()
                total += member.size
                if name in files or total > max_bytes:
                    raise SandboxDenied("output byte limit or duplicate entry")
                handle = archive.extractfile(member)
                if handle is None:
                    raise SandboxDenied("unreadable output entry")
                data = handle.read(member.size + 1)
                if len(data) != member.size:
                    raise SandboxDenied("truncated output entry")
                files[name] = data
    except (tarfile.TarError, OSError) as exc:
        raise SandboxDenied("invalid output archive") from exc
    return files


@dataclass(frozen=True)
class SandboxRequest:
    image: str
    source_dir: Path
    source_digest: dict
    input_dir: Path
    input_digest: dict
    argv: tuple[str, ...]
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    egress: str = "none"

    def __post_init__(self):
        if not isinstance(self.image, str) or not re.fullmatch(r"([a-f0-9]{64}|sha256:[a-f0-9]{64})", self.image):
            raise SandboxDenied("immutable locally-approved image ID required")
        if self.egress != "none":
            raise SandboxDenied("proxy-only egress is not implemented")
        if (not isinstance(self.argv, tuple) or not self.argv
                or any(not isinstance(x, str) or not x or "\x00" in x for x in self.argv)
                or not self.argv[0].startswith("/")):
            raise SandboxDenied("absolute container entrypoint and argument tuple required")
        if not isinstance(self.limits, SandboxLimits):
            raise SandboxDenied("validated sandbox limits required")
        for digest in (self.source_digest, self.input_digest):
            if not isinstance(digest, dict) or digest.get("algorithm") != "sha256" or not digest.get("files"):
                raise SandboxDenied("immutable snapshot digests required")


@dataclass(frozen=True)
class SandboxResult:
    state: str
    exit_code: int | None = None
    code: str | None = None
    output_files: dict[str, bytes] = field(default_factory=dict)
    stdout_tail: str = ""
    stderr_tail: str = ""
    cleanup: str = "not_started"
    container_name: str | None = None
    engine_version: str | None = None


@dataclass(frozen=True)
class _Command:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    interrupted: str | None = None


class PodmanSandbox:
    """Control Podman only; agent commands never execute on the host.

    The approved image must contain /bin/sleep and the declared entrypoint.
    No image is pulled or built here. The caller owns image approval.
    """
    def __init__(self, *, environment: Mapping[str, str] | None = None):
        env = dict(os.environ if environment is None else environment)
        socket = env.get("ENGINE_SOCKET")
        if socket and (_socket_override(socket) != socket or _is_root_connection(socket)):
            raise SandboxDenied("rootless Unix socket required")
        self.prefix = ["podman", "--url", socket] if socket else ["podman"]
        # Podman needs the user's connection config, not provider/DB credentials.
        self.env = {k: env[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME") if k in env}

    def _command(self, args, *, timeout=COMMAND_TIMEOUT, cap=STREAM_CAP_BYTES,
                 should_cancel: Callable[[], bool] = lambda: False):
        proc = subprocess.Popen([*self.prefix, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env, shell=False)
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()

        def drain(pipe, buffer):
            try:
                while chunk := pipe.read(65536):
                    room = max(0, cap - len(buffer))
                    buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        overflow.set()
            finally:
                pipe.close()

        threads = [threading.Thread(target=drain, args=(pipe, buf), daemon=True)
                   for pipe, buf in zip((proc.stdout, proc.stderr), buffers)]
        for thread in threads:
            thread.start()
        deadline, interrupted = time.monotonic() + timeout, None
        try:
            while proc.poll() is None:
                if should_cancel():
                    interrupted = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    interrupted = "timed_out"
                    break
                time.sleep(.05)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=COMMAND_TIMEOUT)
            for thread in threads:
                thread.join(timeout=COMMAND_TIMEOUT)
        return _Command(proc.returncode, bytes(buffers[0]), bytes(buffers[1]),
                        overflow.is_set(), interrupted)

    def validate_snapshot(self, directory: Path, expected: dict):
        if snapshot_digest(directory) != expected:
            raise SandboxDenied("snapshot digest mismatch")

    def run_argument_vector(self, request, name, source, inputs):
        if not re.fullmatch(r"[a-z0-9-]{1,100}", name):
            raise SandboxDenied("invalid container name")
        for path in (source, inputs):
            if any(x in str(path) for x in (",", "\n", "\x00")):
                raise SandboxDenied("invalid staging path")
        limits = request.limits
        return [
            "create", "--name", name, "--pull=never", "--network=none",
            "--read-only", "--read-only-tmpfs=false", "--user=65532:65532",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--image-volume=ignore", "--unsetenv-all", "--log-driver=none",
            f"--memory={limits.memory_mb}m", f"--memory-swap={limits.memory_mb}m",
            f"--cpus={float(limits.cpus)}", f"--pids-limit={limits.pids}",
            "--shm-size=1m", "--workdir=/source",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin", "--env=HOME=/tmp",
            "--env=LLM_AGENT_EVAL_INPUT=/input", "--env=LLM_AGENT_EVAL_OUTPUT=/output",
            "--env=LLM_AGENT_EVAL_TRACE=/output/trace.jsonl",
            "--tmpfs", f"/output:rw,size={limits.output_mb * 1048576},mode=1777,nosuid,nodev,noexec",
            "--tmpfs", "/tmp:rw,size=16777216,mode=1777,nosuid,nodev,noexec",
            "--mount", f"type=bind,source={source},target=/source,readonly",
            "--mount", f"type=bind,source={inputs},target=/input,readonly",
            "--entrypoint=/bin/sleep", request.image, "infinity",
        ]

    def _cleanup(self, name):
        try:
            exists = self._command(["container", "exists", name])
            if exists.returncode == 1 and not exists.interrupted:
                return "complete"
            if exists.returncode != 0 or exists.interrupted:
                return "failed"
            removed = self._command(["rm", "--force", "--time", "0", name])
            absent = self._command(["container", "exists", name])
            return "complete" if (removed.returncode == 0 and not removed.interrupted
                and absent.returncode == 1 and not absent.interrupted) else "failed"
        except (OSError, subprocess.SubprocessError):
            return "failed"

    def run(self, request: SandboxRequest, *, should_cancel: Callable[[], bool] = lambda: False):
        name = "llm-agent-eval-case-" + uuid.uuid4().hex
        result = SandboxResult("blocked_setup", code="runtime_unavailable")
        created = False
        version = None
        try:
            info = self._command(["info", "--format", "json"])
            if info.returncode or info.interrupted or info.truncated:
                return result
            observed = json.loads(info.stdout)
            host = observed.get("host", {})
            if host.get("security", {}).get("rootless") is not True:
                return replace(result, code="rootless_runtime_required")
            if (host.get("cgroupVersion") != "v2"
                    or not {"cpu", "memory", "pids"} <= set(host.get("cgroupControllers", []))):
                return replace(result, code="resource_limits_unavailable")
            version = observed.get("version", {}).get("Version")
            image = self._command(["image", "exists", request.image])
            if image.returncode or image.interrupted:
                return replace(result, code="image_unavailable", engine_version=version)
            if should_cancel():
                return replace(result, state="cancelled", code="cancelled", engine_version=version)
            self.validate_snapshot(request.source_dir, request.source_digest)
            self.validate_snapshot(request.input_dir, request.input_digest)
            with tempfile.TemporaryDirectory(prefix="llm-agent-eval-sandbox-") as staging:
                source, inputs = Path(staging) / "source", Path(staging) / "input"
                shutil.copytree(request.source_dir, source, symlinks=True)
                shutil.copytree(request.input_dir, inputs, symlinks=True)
                self.validate_snapshot(source, request.source_digest)
                self.validate_snapshot(inputs, request.input_digest)
                # The remote Podman VM must be able to read these staged copies.
                Path(staging).chmod(0o755)
                for root in (source, inputs):
                    root.chmod(0o755)
                    for path in root.rglob("*"):
                        path.chmod(0o755 if path.is_dir() else 0o644)
                created = True  # timed-out creation can still create a container
                create = self._command(self.run_argument_vector(request, name, source, inputs))
                if create.returncode or create.interrupted:
                    result = replace(result, code="container_create_failed")
                else:
                    start = self._command(["start", name])
                    if start.returncode or start.interrupted:
                        result = replace(result, code="container_start_failed")
                    else:
                        execution = self._command(["exec", name, *request.argv],
                            timeout=request.limits.timeout_seconds, should_cancel=should_cancel)
                        state = execution.interrupted or ("completed" if execution.returncode == 0 else "invocation_failed")
                        result = SandboxResult(state, execution.returncode,
                            stdout_tail=execution.stdout.decode("utf-8", "replace"),
                            stderr_tail=execution.stderr.decode("utf-8", "replace"),
                            code="output_truncated" if execution.truncated else None)
                        if state == "completed":
                            size = request.limits.output_mb * 1048576
                            archive = self._command(["cp", f"{name}:/output/.", "-"],
                                cap=size + MAX_TREE_FILES * 1024 + 10240)
                            if archive.returncode or archive.interrupted or archive.truncated:
                                result = replace(result, state="invocation_failed", code="output_collection_failed")
                            else:
                                result = replace(result, output_files=read_output_archive(archive.stdout, size))
                # Remove while bind sources still exist, then release staging.
                cleanup = self._cleanup(name)
                created = False
                result = replace(result, cleanup=cleanup)
        except SandboxDenied:
            result = replace(result, state="blocked_setup", code="sandbox_denied")
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            result = replace(result, state="blocked_setup", code="runtime_unavailable")
        finally:
            if created:
                result = replace(result, cleanup=self._cleanup(name))
        if result.cleanup == "failed":
            result = replace(result, state="invocation_failed", code="cleanup_failed", output_files={})
        return replace(result, container_name=name if result.cleanup != "not_started" else None,
                       engine_version=version)
