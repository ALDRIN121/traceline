"""Trusted, bounded redaction used before data crosses a persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


REDACTION_VERSION = "2026-09-01"
_CANARY = re.compile(r"sk-test-do-not-persist-[A-Za-z0-9_-]+")
_PROVIDER_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNED_SECRET = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+")
_SENSITIVE_KEY = re.compile(r"(?i)(authorization|api[_-]?key|access[_-]?token|password|secret|credential)")


@dataclass(frozen=True)
class RedactedPayload:
    content: Any
    version: str
    detector_flags: tuple[str, ...]
    truncated: bool = False


def redact(payload: Any, policy: Any = None, *, max_string_bytes: int = 16 * 1024) -> RedactedPayload:
    """Return a safe, JSON-like copy; never retain an unbounded secret-bearing string."""
    if not isinstance(max_string_bytes, int) or max_string_bytes < 32:
        raise ValueError("max_string_bytes must be at least 32")
    flags: set[str] = set()
    truncated = False

    def clean_text(value: str) -> str:
        nonlocal truncated
        if _CANARY.search(value):
            flags.add("canary")
            value = _CANARY.sub("[REDACTED]", value)
        if _PROVIDER_TOKEN.search(value):
            flags.add("token")
            value = _PROVIDER_TOKEN.sub("[REDACTED]", value)
        if _BEARER.search(value):
            flags.add("token")
            value = _BEARER.sub("Bearer [REDACTED]", value)
        if _ASSIGNED_SECRET.search(value):
            flags.add("secret")
            value = _ASSIGNED_SECRET.sub(lambda m: m.group(1) + "=[REDACTED]", value)
        encoded = value.encode("utf-8")
        if len(encoded) > max_string_bytes:
            truncated = True
            suffix = "…[TRUNCATED]"
            limit = max_string_bytes - len(suffix.encode("utf-8"))
            value = encoded[:max(0, limit)].decode("utf-8", errors="ignore") + suffix
        return value

    def walk(value: Any, *, sensitive: bool = False) -> Any:
        if sensitive:
            flags.add("header")
            return "[REDACTED]"
        if isinstance(value, BaseException):
            flags.add("exception")
            return clean_text(f"{type(value).__name__}: {value}")
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, bytes):
            flags.add("binary")
            return f"[BINARY {len(value)} bytes]"
        if isinstance(value, Mapping):
            return {str(key): walk(item, sensitive=bool(_SENSITIVE_KEY.search(str(key)))) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [walk(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return clean_text(repr(value))

    return RedactedPayload(walk(payload), REDACTION_VERSION, tuple(sorted(flags)), truncated)
