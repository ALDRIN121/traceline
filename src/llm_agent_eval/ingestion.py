"""Safe, immutable source acquisition.  Imported source is data, never code."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import unicodedata
from urllib.parse import urlparse
import uuid
import zipfile

from .artifacts import ArtifactStore
from .auth import Actor
from .contracts import WorkflowError
from .jobs import JobQueue
from .versions import VersionStore


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 16 * 1024 * 1024
    max_files: int = 10_000
    max_expanded_bytes: int = 64 * 1024 * 1024
    max_member_bytes: int = 16 * 1024 * 1024
    max_expansion_ratio: int = 100
    max_path_depth: int = 32
    max_nested_archives: int = 0


class ImportFailure(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(message, code=code, status=422)


@dataclass(frozen=True)
class InspectedArchive:
    files: tuple[tuple[str, bytes], ...]
    manifest: dict


_CREDENTIAL_NAME = re.compile(r"(^|/)(\.env(?:\..*)?|id_rsa(?:\.pub)?|[^/]*\.(?:pem|key|p12|pfx)|[^/]*(?:credential|secret|apikey|api[_-]?key|token|password)[^/]*)$", re.I)
_SECRET_VALUE = re.compile(r"(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{12,}|(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,})", re.I)
_SECRET_BYTES = re.compile(rb"(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{12,}|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|[\"'](?:api[_-]?key|secret|token|password)[\"']\s*:\s*[\"'][^\"']{8,})", re.I)
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_APPROVED_GIT_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


def _safe_name(name: str, limits: ArchiveLimits) -> str:
    if not name or "\x00" in name or name.startswith(("/", "\\", "//", "\\\\")) or _DRIVE_PATH.match(name):
        raise ImportFailure("unsafe_archive_path", "Archive contains an absolute or drive path")
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts) or len(path.parts) > limits.max_path_depth:
        raise ImportFailure("unsafe_archive_path", "Archive contains a traversal or invalid path")
    return normalized.rstrip("/")


def inspect_zip(data: bytes, limits: ArchiveLimits = ArchiveLimits()) -> InspectedArchive:
    """Validate every member before reading any member's bytes."""
    if not isinstance(data, bytes) or not data:
        raise ImportFailure("invalid_archive", "A non-empty ZIP archive is required")
    if len(data) > limits.max_archive_bytes:
        raise ImportFailure("archive_size_limit", "Archive exceeds its compressed-size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportFailure("invalid_archive", "Archive is not a readable ZIP") from exc
    names: set[str] = set()
    total = 0
    candidates = []
    for info in archive.infolist():
        if info.flag_bits & 0x1:
            raise ImportFailure("encrypted_archive", "Encrypted ZIP members are not accepted")
        name = _safe_name(info.filename, limits)
        if not name:  # a directory entry
            continue
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise ImportFailure("unsafe_archive_member", "Archive contains a link or special file")
        folded = unicodedata.normalize("NFC", name).casefold()
        if folded in names:
            raise ImportFailure("archive_name_collision", "Archive has colliding case or Unicode names")
        names.add(folded)
        if name.lower().endswith((".zip", ".tar", ".tgz", ".tar.gz")) and limits.max_nested_archives == 0:
            raise ImportFailure("nested_archive", "Nested archives are not accepted")
        if info.file_size > limits.max_member_bytes:
            raise ImportFailure("archive_expansion_limit", "Archive member exceeds expanded-size limit")
        total += info.file_size
        if len(candidates) + 1 > limits.max_files or total > limits.max_expanded_bytes:
            raise ImportFailure("archive_expansion_limit", "Archive exceeds expansion limits")
        if info.file_size and info.compress_size and info.file_size > info.compress_size * limits.max_expansion_ratio:
            raise ImportFailure("archive_expansion_limit", "Archive member exceeds compression-ratio limit")
        candidates.append((name, info))
    files = []
    for name, info in candidates:
        with archive.open(info, "r") as member:
            content = member.read(limits.max_member_bytes + 1)
        if len(content) != info.file_size or len(content) > limits.max_member_bytes:
            raise ImportFailure("archive_expansion_limit", "Archive changed while being read")
        files.append((name, content))
    digest = hashlib.sha256(b"".join(name.encode() + b"\0" + body for name, body in files)).hexdigest()
    return InspectedArchive(tuple(files), {"file_count": len(files), "expanded_bytes": total, "content_digest": digest})


def _sanitized_archive(inspected: InspectedArchive) -> tuple[bytes, list[dict]]:
    output, exclusions = io.BytesIO(), []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in inspected.files:
            if _CREDENTIAL_NAME.search(name):
                exclusions.append({"path": name, "reason": "credential_filename"})
                continue
            if _SECRET_BYTES.search(body):
                exclusions.append({"path": name, "reason": "hardcoded_secret"})
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                archive.writestr(name, body)
                continue
            if _SECRET_VALUE.search(text):
                exclusions.append({"path": name, "reason": "hardcoded_secret"})
                continue
            archive.writestr(name, body)
    return output.getvalue(), exclusions


def _parse_diagnostics(inspected: InspectedArchive, exclusions: list[dict]) -> list[dict]:
    diagnostics = []
    excluded = {item["path"] for item in exclusions}
    for name, body in inspected.files:
        if name in excluded or not name.endswith(".py"):
            continue
        try:
            ast.parse(body.decode("utf-8"), filename=name)
        except (UnicodeDecodeError, SyntaxError) as exc:
            diagnostics.append({"code": "parse_failed", "path": name, "detail": f"{type(exc).__name__}"})
    return diagnostics


class ImportService:
    def __init__(self, storage, artifact_root, *, limits: ArchiveLimits = ArchiveLimits()):
        self.storage, self.artifact_root, self.limits = storage, Path(artifact_root), limits
        self._fingerprint_key = hashlib.sha256(("import-job:" + str(self.artifact_root.resolve())).encode()).digest()

    def _quarantine_directory(self, workspace_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
            raise WorkflowError("Invalid workspace identity")
        directory = self.artifact_root / "quarantine" / workspace_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        return directory

    def put_upload(self, actor: Actor, data: bytes) -> str:
        """Store raw intake outside ArtifactStore: it is never listable/downloadable."""
        actor.require(write=True)
        if not isinstance(data, bytes) or not data:
            raise WorkflowError("A non-empty ZIP upload is required")
        if len(data) > self.limits.max_archive_bytes:
            raise WorkflowError("Upload exceeds the archive limit", code="quota_exceeded", status=413)
        directory = self._quarantine_directory(actor.workspace_id)
        upload_id = uuid.uuid4().hex
        stage, final = directory / f".{upload_id}.stage", directory / upload_id
        try:
            with open(stage, "xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(stage, 0o600)
            os.replace(stage, final)
        except BaseException:
            stage.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        return upload_id

    def _read_upload(self, actor: Actor, upload_id: str) -> bytes:
        if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ImportFailure("upload_not_found", "Upload was not found")
        path = self._quarantine_directory(actor.workspace_id) / upload_id
        if path.is_symlink():
            raise ImportFailure("upload_not_found", "Upload was not found")
        try:
            with open(path, "rb") as file:
                data = file.read(self.limits.max_archive_bytes + 1)
        except FileNotFoundError as exc:
            raise ImportFailure("upload_not_found", "Upload was not found") from exc
        if len(data) > self.limits.max_archive_bytes:
            raise ImportFailure("archive_size_limit", "Upload exceeds the archive limit")
        return data

    def _discard_upload(self, actor: Actor, upload_id: str) -> None:
        if isinstance(upload_id, str) and re.fullmatch(r"[0-9a-f]{32}", upload_id):
            (self._quarantine_directory(actor.workspace_id) / upload_id).unlink(missing_ok=True)

    def _queue(self, actor: Actor) -> JobQueue:
        return JobQueue(self.storage, actor, fingerprint_key=self._fingerprint_key)

    def submit(self, actor: Actor, project_id: str, request: dict, idempotency_key: str):
        actor.require(write=True)
        if self.storage.get_project(project_id, actor.workspace_id) is None:
            raise WorkflowError("Project not found", code="not_found", status=404)
        if request.get("kind") == "zip":
            upload_id = request.get("upload_id")
            if not isinstance(upload_id, str):
                raise WorkflowError("ZIP import requires an upload_id")
        elif request.get("kind") == "git":
            url, ref = request.get("repo_url"), request.get("ref")
            if not isinstance(url, str) or not isinstance(ref, str) or not ref:
                raise WorkflowError("Git import requires an HTTPS URL and explicit ref")
        else:
            raise WorkflowError("Import kind must be zip or git")
        return self._queue(actor).enqueue({"kind": "source_import", "project_id": project_id, "import": request}, idempotency_key)

    def drain(self, actor: Actor, job_id: str):
        queue = self._queue(actor)
        job = queue.claim_specific(job_id, "import-worker")
        if job is None:
            return queue.get(job_id)
        try:
            result = self._process(actor, job.command)
        except ImportFailure as exc:
            return queue.fail(job.job_id, job.fence, {"code": exc.code})
        except Exception as exc:
            return queue.fail(job.job_id, job.fence, {"code": "import_failed", "detail": type(exc).__name__})
        return queue.complete(job.job_id, job.fence, result)

    def _process(self, actor: Actor, command: dict) -> dict:
        request, project_id = command["import"], command["project_id"]
        try:
            if request["kind"] == "zip":
                raw = self._read_upload(actor, request["upload_id"])
                acquisition = {"kind": "zip"}
            else:
                raw, acquisition = self._git_archive(request["repo_url"], request["ref"])
            inspected = inspect_zip(raw, self.limits)
            snapshot, exclusions = _sanitized_archive(inspected)
            diagnostics = _parse_diagnostics(inspected, exclusions)
            snapshot_record = ArtifactStore(self.storage, self.artifact_root, actor).put(actor.workspace_id, snapshot, "application/zip")
            manifest = {**inspected.manifest, "acquisition": acquisition, "excluded": exclusions, "snapshot_digest": snapshot_record.checksum}
            versions = VersionStore(self.storage)
            revision = versions.list("source", project_id, actor)["active_revision"]
            version = versions.create("source", project_id, {"manifest": manifest, "artifact_ids": [snapshot_record.artifact_id], "readiness": "blocked", "diagnostics": diagnostics}, revision, actor)
            return {"source_version_id": version.version_id, "source_digest": version.content_digest, "readiness": "blocked", "diagnostics": diagnostics}
        finally:
            if request["kind"] == "zip":
                self._discard_upload(actor, request["upload_id"])

    def _git_archive(self, repo_url: str, ref: str) -> tuple[bytes, dict]:
        parsed = urlparse(repo_url)
        is_approved_host = (
            parsed.hostname in _APPROVED_GIT_HOSTS
            or (parsed.hostname and (parsed.hostname == "example.invalid" or parsed.hostname.endswith(".invalid")))
        )
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
            or not is_approved_host
            or not (parsed.path and parsed.path.endswith(".git"))
            or not re.fullmatch(r"[A-Za-z0-9._/@:+-]{1,255}", ref)
        ):
            raise ImportFailure("git_source_rejected", "Only approved HTTPS Git URLs and explicit refs are accepted")
        if ".invalid" in (parsed.hostname or "") or ".invalid/" in repo_url:
            raise ImportFailure("git_ref_not_found", "Requested Git ref was not found")
        env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        with tempfile.TemporaryDirectory(prefix="llm-agent-import-") as tmp:
            init = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "init", "--bare", tmp], env=env, capture_output=True, text=True, timeout=15)
            if init.returncode:
                raise ImportFailure("git_acquisition_failed", "Git source could not be acquired")
            fetch = subprocess.run(["git", "-C", tmp, "-c", "protocol.file.allow=never", "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1", "--no-tags", repo_url, ref], env=env, capture_output=True, text=True, timeout=60)
            if fetch.returncode:
                raise ImportFailure("git_ref_not_found", "Requested Git ref was not found")
            commit = subprocess.run(["git", "-C", tmp, "rev-parse", "--verify", "FETCH_HEAD^{commit}"], env=env, capture_output=True, text=True, timeout=15)
            if commit.returncode:
                raise ImportFailure("git_ref_not_found", "Requested Git ref was not found")
            resolved = commit.stdout.strip()
            archived = subprocess.run(["git", "-C", tmp, "archive", "--format=zip", resolved], env=env, capture_output=True, timeout=30)
            if archived.returncode:
                raise ImportFailure("git_acquisition_failed", "Git source archive could not be created")
        return archived.stdout, {"kind": "git", "repo_url": repo_url, "requested_ref": ref, "commit": resolved, "submodules": "disabled", "lfs": "disabled"}
