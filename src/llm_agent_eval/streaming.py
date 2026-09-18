"""Bounded SSE parsing for R2 stream targets."""

from __future__ import annotations

from dataclasses import dataclass


class StreamProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class StreamFrame:
    event: str
    data: str
    id: str | None = None


class SSEParser:
    def __init__(self, *, max_frame_bytes: int = 64 * 1024, max_frames: int = 10_000):
        self.max_frame_bytes, self.max_frames = max_frame_bytes, max_frames
        self._buffer = bytearray()
        self._fields: list[tuple[str, str]] = []
        self._frames = 0

    def feed(self, chunk: bytes) -> list[StreamFrame]:
        if not isinstance(chunk, bytes):
            raise StreamProtocolError("SSE chunks must be bytes")
        self._buffer.extend(chunk)
        if len(self._buffer) > self.max_frame_bytes:
            raise StreamProtocolError("SSE frame exceeds byte limit")
        frames = []
        while b"\n" in self._buffer:
            line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            line = line.rstrip(b"\r")
            if line == b"":
                if self._fields:
                    frames.append(self._finish())
                continue
            if line.startswith(b":"):
                continue
            try:
                field, value = line.decode("utf-8").split(":", 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise StreamProtocolError("malformed SSE field") from exc
            if field not in {"event", "data", "id"}:
                raise StreamProtocolError("unsupported SSE field")
            self._fields.append((field, value[1:] if value.startswith(" ") else value))
        return frames

    def finish(self) -> list[StreamFrame]:
        if self._buffer or self._fields:
            raise StreamProtocolError("stream ended with an incomplete SSE frame")
        return []

    def _finish(self) -> StreamFrame:
        self._frames += 1
        if self._frames > self.max_frames:
            raise StreamProtocolError("SSE frame count exceeded")
        event = next((value for field, value in self._fields if field == "event"), "message")
        data = "\n".join(value for field, value in self._fields if field == "data")
        frame_id = next((value for field, value in self._fields if field == "id"), None)
        self._fields = []
        return StreamFrame(event, data, frame_id)
