"""Persistent multi-turn Agent chat sessions for the PC popup (matches AgentSessionStore.kt)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
import uuid

from overlay.timeline import (
    TimelineEntry,
    from_legacy,
    parse_timeline,
    serialize_timeline,
    thinking_text,
    tool_calls,
)

LOGGER = logging.getLogger("localwhisper.pc_sessions")

MAX_SAVED_SESSIONS = 20
MAX_TURNS_PER_SESSION = 30
MAX_TITLE_CHARS = 34


def default_storage_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else (Path.home() / ".localwhisper")
    return base / "LocalWhisper"


@dataclass(frozen=True)
class AgentChatTurn:
    user_prompt: str
    reply: str
    timeline: list[TimelineEntry] = field(default_factory=list)

    @property
    def thinking(self) -> str:
        return thinking_text(self.timeline)

    @property
    def tools(self) -> list[dict[str, str]]:
        return tool_calls(self.timeline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_prompt": self.user_prompt,
            "reply": self.reply,
            "tool_calls": self.tools,
            "timeline": serialize_timeline(self.timeline),
        }

    @staticmethod
    def from_dict(obj: dict[str, Any]) -> AgentChatTurn:
        raw_tools = obj.get("tool_calls") if isinstance(obj.get("tool_calls"), list) else []
        tl = parse_timeline(obj.get("timeline"))
        if not tl:
            tl = from_legacy(str(obj.get("thinking", "")), raw_tools)
        return AgentChatTurn(
            user_prompt=str(obj.get("user_prompt", "")),
            reply=str(obj.get("reply", "")),
            timeline=tl,
        )


@dataclass(frozen=True)
class SavedAgentSession:
    session_id: str
    title: str
    updated_at_ms: int
    turns: list[AgentChatTurn]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "updated_at_ms": self.updated_at_ms,
            "turns": [t.to_dict() for t in self.turns],
        }

    def to_history_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for turn in self.turns:
            if turn.user_prompt.strip():
                messages.append({"role": "user", "content": turn.user_prompt})
            if turn.reply.strip():
                messages.append({"role": "assistant", "content": turn.reply})
        return messages


def _build_title(first_prompt: str) -> str:
    one_line = " ".join(str(first_prompt or "").split())
    if not one_line:
        return "Agent chat"
    if len(one_line) <= MAX_TITLE_CHARS:
        return one_line
    return one_line[:MAX_TITLE_CHARS].rstrip() + "…"


class PcSessionStore:
    """Stores up to 20 multi-turn Agent sessions on disk as JSON."""

    def __init__(self, path: Path | None = None, clock: Any = time.time) -> None:
        self.path = path or (default_storage_dir() / "overlay_sessions.json")
        self._clock = clock
        self.active_session_id: str | None = None
        self.last_closed_at_ms: int = 0
        self._sessions: list[SavedAgentSession] = []
        self._load()

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def record_overlay_closed(self) -> None:
        self.last_closed_at_ms = self._now_ms()
        self._save()

    def resolve_initial_session_id(self, resume_timeout_seconds: int) -> str | None:
        current_id = self.active_session_id
        if not current_id or self.get_session(current_id) is None:
            self.active_session_id = None
            return None
        if resume_timeout_seconds <= 0 or self.last_closed_at_ms <= 0:
            self.active_session_id = None
            return None
        elapsed_ms = self._now_ms() - self.last_closed_at_ms
        if 0 <= elapsed_ms <= resume_timeout_seconds * 1000:
            return current_id
        self.active_session_id = None
        return None

    def list_sessions(self) -> list[SavedAgentSession]:
        return sorted(self._sessions, key=lambda s: s.updated_at_ms, reverse=True)

    def get_session(self, session_id: str | None) -> SavedAgentSession | None:
        if not session_id:
            return None
        return next((s for s in self._sessions if s.session_id == session_id), None)

    def start_new_session(self) -> str:
        new_id = uuid.uuid4().hex[:12]
        self.active_session_id = None
        self._save()
        return new_id

    def record_turn(self, session_id: str, turn: AgentChatTurn) -> SavedAgentSession:
        clean_id = (session_id or "").strip() or self.start_new_session()
        now_ms = self._now_ms()
        existing = self.get_session(clean_id)
        self._sessions = [s for s in self._sessions if s.session_id != clean_id]
        if existing is not None:
            turns = (existing.turns + [turn])[-MAX_TURNS_PER_SESSION:]
            updated = SavedAgentSession(clean_id, existing.title, now_ms, turns)
        else:
            updated = SavedAgentSession(clean_id, _build_title(turn.user_prompt), now_ms, [turn])
        self._sessions.insert(0, updated)
        self._sessions = self.list_sessions()[:MAX_SAVED_SESSIONS]
        self.active_session_id = clean_id
        self._save()
        return updated

    def update_last_turn(self, session_id: str, updated_turn: AgentChatTurn) -> SavedAgentSession:
        existing = self.get_session(session_id)
        if existing is None or not existing.turns:
            return self.record_turn(session_id, updated_turn)
        self._sessions = [s for s in self._sessions if s.session_id != session_id]
        turns = list(existing.turns)
        prev = turns[-1]
        prompt = updated_turn.user_prompt.strip() or prev.user_prompt
        turns[-1] = AgentChatTurn(user_prompt=prompt, reply=updated_turn.reply, timeline=updated_turn.timeline)
        updated = SavedAgentSession(session_id, existing.title, self._now_ms(), turns)
        self._sessions.insert(0, updated)
        self._sessions = self.list_sessions()[:MAX_SAVED_SESSIONS]
        self.active_session_id = session_id
        self._save()
        return updated

    def clear_all(self) -> None:
        self._sessions.clear()
        self.active_session_id = None
        self.last_closed_at_ms = 0
        self._save()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.active_session_id = str(data.get("active_session_id", "") or "").strip() or None
        self.last_closed_at_ms = int(data.get("last_closed_at_ms", 0) or 0)
        raw_list = data.get("sessions")
        if not isinstance(raw_list, list):
            return
        loaded: list[SavedAgentSession] = []
        for item in raw_list[:MAX_SAVED_SESSIONS]:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id", "") or "").strip()
            raw_turns = item.get("turns")
            if not sid or not isinstance(raw_turns, list):
                continue
            turns = [AgentChatTurn.from_dict(t) for t in raw_turns[:MAX_TURNS_PER_SESSION] if isinstance(t, dict)]
            if turns:
                loaded.append(
                    SavedAgentSession(
                        session_id=sid,
                        title=str(item.get("title", "Agent chat")),
                        updated_at_ms=int(item.get("updated_at_ms", 0) or 0),
                        turns=turns,
                    )
                )
        self._sessions = sorted(loaded, key=lambda s: s.updated_at_ms, reverse=True)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active_session_id": self.active_session_id or "",
                "last_closed_at_ms": self.last_closed_at_ms,
                "sessions": [s.to_dict() for s in self.list_sessions()[:MAX_SAVED_SESSIONS]],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            LOGGER.warning("Could not save PC overlay sessions to %s: %s", self.path, exc)
