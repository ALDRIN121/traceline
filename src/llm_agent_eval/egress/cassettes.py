"""Versioned record/replay worlds with deterministic request fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from ..redaction import redact
from ..streaming import SSEParser


def cassette_fingerprint(provider: str, path: str, request: Any, version: str) -> str:
    encoded = json.dumps({"provider": provider, "path": path, "request": request, "version": version},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class CassetteWorld:
    def __init__(self, root: Path, *, mode: str, version: str = "v1"):
        if mode not in {"record", "replay", "hybrid"}:
            raise ValueError("cassette mode must be record, replay, or hybrid")
        self.root, self.mode, self.version = Path(root), mode, version
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, fingerprint: str) -> Path:
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("invalid cassette fingerprint")
        return self.root / f"{fingerprint}.json"

    def exchange(self, fingerprint: str, request: Any, send: Callable[[], tuple[int, Any]]):
        path = self._path(fingerprint)
        if self.mode in {"replay", "hybrid"} and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != self.version:
                raise ValueError("cassette version mismatch")
            return int(payload["status"]), payload["body"]
        if self.mode == "replay":
            raise KeyError(fingerprint)
        status, body = send()
        safe_body = redact(body).content
        encoded = json.dumps({"version": self.version, "status": status, "body": safe_body},
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 1_048_576:
            raise ValueError("cassette response exceeds byte limit")
        path.write_text(encoded, encoding="utf-8")
        return status, safe_body

    def exchange_stream(self, fingerprint: str, request: Any, send: Callable[[], tuple[int, Any]]):
        """Record/replay a bounded SSE stream as redacted canonical frames."""
        path = self._path(fingerprint)
        if self.mode in {"replay", "hybrid"} and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != self.version:
                raise ValueError("cassette version mismatch")
            if payload.get("stream") is not True or not isinstance(payload.get("frames"), list):
                raise ValueError("cassette stream shape mismatch")
            return int(payload["status"]), iter(_encode_frames(payload["frames"]))
        if self.mode == "replay":
            raise KeyError(fingerprint)
        status, raw_stream = send()
        parser = SSEParser()
        frames = []
        for chunk in raw_stream:
            if not isinstance(chunk, bytes):
                raise ValueError("cassette stream chunks must be bytes")
            for frame in parser.feed(chunk):
                frames.append(_safe_frame(frame))
        parser.finish()
        encoded = json.dumps(
            {"version": self.version, "stream": True, "status": status, "frames": frames},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 1_048_576:
            raise ValueError("cassette response exceeds byte limit")
        path.write_text(encoded, encoding="utf-8")
        return status, iter(_encode_frames(frames))


def _safe_frame(frame) -> dict[str, Any]:
    if frame.data == "[DONE]":
        data = frame.data
    else:
        try:
            data = json.dumps(
                redact(json.loads(frame.data)).content,
                sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            )
        except (TypeError, ValueError):
            data = redact(frame.data).content
    return {"event": frame.event, "data": data, "id": frame.id}


def _encode_frames(frames: list[dict[str, Any]]):
    for frame in frames:
        lines = []
        if frame.get("event") and frame["event"] != "message":
            lines.append(f"event: {frame['event']}")
        if frame.get("id") is not None:
            lines.append(f"id: {frame['id']}")
        lines.append(f"data: {frame.get('data', '')}")
        yield ("\n".join(lines) + "\n\n").encode("utf-8")
