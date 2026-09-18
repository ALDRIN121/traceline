"""Restricted, digest-pinned local runtime image preparation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable, Mapping
import uuid

from .sandbox import SandboxDenied, snapshot_digest


class BuildDenied(ValueError):
    """A source or build request cannot meet the restricted-build contract."""


@dataclass(frozen=True)
class RuntimeProfile:
    base_image: str
    entrypoint: tuple[str, ...]
    policy_version: str = "r1-restricted-build-v1"
    egress: str = "none"

    def __post_init__(self):
        if not isinstance(self.base_image, str) or not self.base_image.startswith("sha256:"):
            raise BuildDenied("a digest-pinned base image is required")
        if len(self.base_image) != 71 or any(c not in "0123456789abcdef" for c in self.base_image[7:]):
            raise BuildDenied("base image digest is invalid")
        if not isinstance(self.entrypoint, tuple) or not self.entrypoint:
            raise BuildDenied("an absolute container entrypoint is required")
        if any(not isinstance(item, str) or not item or "\x00" in item for item in self.entrypoint):
            raise BuildDenied("entrypoint arguments must be bounded strings")
        if not self.entrypoint[0].startswith("/"):
            raise BuildDenied("entrypoint must be absolute inside the image")
        if self.egress not in {"none", "proxy"}:
            raise BuildDenied("runtime egress must be none or proxy")


@dataclass(frozen=True)
class BuildJob:
    job_id: str
    state: str
    source_digest: str
    image_digest: str
    entrypoint: tuple[str, ...]
    provenance: dict[str, str]


def _run_podman(args: list[str], *, timeout: float, env: Mapping[str, str]):
    return subprocess.run(args, capture_output=True, text=True, check=False,
                          shell=False, timeout=timeout, env=dict(env))


class BuildService:
    """Build only from an engine-owned source snapshot with no ambient secrets."""

    def __init__(self, *, command_runner: Callable | None = None,
                 environment: Mapping[str, str] | None = None):
        source_env = dict(os.environ if environment is None else environment)
        self.environment = {key: source_env[key] for key in ("PATH", "HOME", "TMPDIR") if key in source_env}
        self.command_runner = command_runner or _run_podman

    def prepare(self, source_dir: Path, profile: RuntimeProfile) -> BuildJob:
        source_dir = Path(source_dir)
        try:
            digest = snapshot_digest(source_dir)
        except (OSError, SandboxDenied) as exc:
            raise BuildDenied("source snapshot is not safe for building") from exc
        source_digest = hashlib.sha256(
            json.dumps(digest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        tag = f"llm-agent-eval-build-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="llm-agent-build-") as context_name:
            context = Path(context_name)
            staged = context / "source"
            shutil.copytree(source_dir, staged, symlinks=False)
            containerfile = context / "Containerfile"
            containerfile.write_text(
                f"FROM {profile.base_image}\n"
                "COPY source/ /source/\n"
                "WORKDIR /source\n",
                encoding="utf-8",
            )
            build = [
                "podman", "build", "--pull=never", "--network=none",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--label", f"llm-agent-eval.source-digest={source_digest}",
                "--label", f"llm-agent-eval.build-policy={profile.policy_version}",
                "--tag", tag, "--file", str(containerfile), str(context),
            ]
            try:
                result = self.command_runner(build, timeout=300, env=self.environment)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BuildDenied("restricted image build could not start") from exc
            if result.returncode != 0:
                raise BuildDenied("restricted image build failed")
            inspect = ["podman", "image", "inspect", "--format", "{{.Id}}", tag]
            try:
                result = self.command_runner(inspect, timeout=30, env=self.environment)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BuildDenied("built image identity could not be inspected") from exc
            if result.returncode != 0:
                raise BuildDenied("built image identity could not be inspected")
            image_digest = self._parse_image_digest(result.stdout)
        return BuildJob(
            job_id=uuid.uuid4().hex, state="prepared", source_digest=source_digest,
            image_digest=image_digest, entrypoint=profile.entrypoint,
            provenance={"build_policy_version": profile.policy_version,
                        "base_image": profile.base_image},
        )

    @staticmethod
    def _parse_image_digest(output: str) -> str:
        text = str(output).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                text = str(parsed[0].get("Id", "")).strip()
        except json.JSONDecodeError:
            pass
        if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
            text = "sha256:" + text
        if not text.startswith("sha256:") or len(text) != 71 or any(c not in "0123456789abcdef" for c in text[7:]):
            raise BuildDenied("Podman returned a non-digest image identity")
        return text
