"""Per-install random key material: secret encryption and idempotency keys.

The engine keeps all install-owned secret material outside the repository, in
one install state directory with restrictive file permissions:

- ``secret.key`` — 32 random bytes, the AES-256-GCM root key for
  :class:`~llm_agent_eval.secrets.SecretStore` ciphertext.
- ``fingerprint.key`` — 32 random bytes, the install-owned HMAC key used to
  derive durable job command digests. Random per install replaces the retired
  path-derived digest (a predictable key let anyone recompute command
  fingerprints).

Keys are fully written and fsynced before an exclusive hard-link publication,
so concurrent installers adopt one complete key. Corrupt or wrong-length key
files fail closed rather than being regenerated: regeneration would orphan
existing ciphertext. The caller supplies an install-state path, not a source
or uploaded-artifact directory.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

__all__ = ["load_install_key", "load_fingerprint_key"]

_KEY_BYTES = 32


def _is_windows() -> bool:
    return os.name == "nt"


def _read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if path.is_symlink():
        raise RuntimeError("Install key must not be a symlink")
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError("Install key cannot be opened safely") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            not _is_windows() and info.st_mode & 0o077
        ):
            raise RuntimeError("Install key must be a private regular file")
        key = handle.read(_KEY_BYTES + 1)
    if len(key) != _KEY_BYTES:
        raise RuntimeError("Install key must contain exactly 32 bytes")
    return key


def _load_or_create_key(path: Path) -> bytes:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(_KEY_BYTES)
    # Write to a sibling temporary file, fsync, then atomically publish with a
    # hard link. The destination never exists in a partial state, and a
    # concurrent installer's link loss is a clean signal to adopt their key.
    fd, staging_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staging_name, 0o600)
        try:
            os.link(staging_name, path)
        except FileExistsError:
            # Apply the same validation to the winner's published key.
            return _read_key(path)
    finally:
        try:
            os.unlink(staging_name)
        except FileNotFoundError:
            pass
    return key


def load_install_key(path: Path) -> bytes:
    """Load (or create once) the 32-byte install secret-encryption key."""
    return _load_or_create_key(path)


def load_fingerprint_key(path: Path) -> bytes:
    """Load (or create once) the 32-byte install job-fingerprint HMAC key."""
    return _load_or_create_key(path)
