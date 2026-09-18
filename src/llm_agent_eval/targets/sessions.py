"""Explicit R2 stateful session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Any, Callable


@dataclass
class Session:
    session_id: str
    case_id: str
    repeat_index: int
    turns: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    def __init__(self, *, initialize: Callable[[str], None], close: Callable[[str], None]):
        self._initialize, self._close = initialize, close
        self._sessions: dict[str, Session] = {}

    def start(self, case_id: str, repeat_index: int) -> Session:
        session = Session(uuid.uuid4().hex, case_id, repeat_index)
        self._sessions[session.session_id] = session
        self._initialize(session.session_id)
        return session

    def turn(self, session_id: str, user_turn: dict[str, Any], invoke: Callable[[str, dict], dict]) -> dict:
        session = self._sessions[session_id]
        result = invoke(session_id, user_turn)
        session.turns.append({"user": user_turn, "result": result})
        return result

    def reset(self, session_id: str):
        session = self._sessions[session_id]
        session.turns.clear()
        self._initialize(session_id)

    def close_session(self, session_id: str):
        self._close(session_id)
        self._sessions.pop(session_id, None)
