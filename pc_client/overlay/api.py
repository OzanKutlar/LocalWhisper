"""HTTP & NDJSON streaming client for the PC Jarvis popup."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable

import httpx

from overlay.timeline import (
    ThinkingEntry,
    TimelineEntry,
    append_thinking,
    has_thinking,
    reconcile_tools,
)

MAX_STREAM_LINES = 200_000
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class PendingConfirmationInfo:
    confirmation_id: str
    tool_name: str
    description: str
    preview_image_b64: str = ""
    phone_action: dict[str, Any] | None = None
    auto_execute: bool = False


@dataclass(frozen=True)
class AgentStreamProgress:
    session_id: str
    timeline: list[TimelineEntry]
    reply: str


@dataclass(frozen=True)
class AgentTurnResult:
    status: str
    session_id: str
    thinking: str
    reply: str
    tool_calls: list[dict[str, Any]]
    pending_confirmation: PendingConfirmationInfo | None
    timeline: list[TimelineEntry] = field(default_factory=list)


def _parse_pending_confirmation(raw: Any) -> PendingConfirmationInfo | None:
    if not isinstance(raw, dict):
        return None
    conf_id = str(raw.get("confirmation_id", "") or "").strip()
    if not conf_id:
        return None
    phone_act = raw.get("phone_action")
    return PendingConfirmationInfo(
        confirmation_id=conf_id,
        tool_name=str(raw.get("tool_name", "")),
        description=str(raw.get("description", "")),
        preview_image_b64=str(raw.get("preview_image_jpeg_b64", "") or ""),
        phone_action=phone_act if isinstance(phone_act, dict) else None,
        auto_execute=bool(raw.get("auto_execute", False)),
    )


def _parse_agent_response(data: dict[str, Any], timeline: list[TimelineEntry]) -> AgentTurnResult:
    raw_tools = data.get("tool_calls") if isinstance(data.get("tool_calls"), list) else []
    merged_tl = list(timeline)
    reconcile_tools(merged_tl, raw_tools)
    thinking_str = str(data.get("thinking", "") or "")
    if thinking_str.strip() and not has_thinking(merged_tl):
        merged_tl.insert(0, ThinkingEntry(thinking_str.strip()))
    return AgentTurnResult(
        status=str(data.get("status", "completed")),
        session_id=str(data.get("session_id", "")),
        thinking=thinking_str,
        reply=str(data.get("reply", "")),
        tool_calls=raw_tools,
        pending_confirmation=_parse_pending_confirmation(data.get("pending_confirmation")),
        timeline=merged_tl,
    )


def _extract_error(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
        if detail:
            return str(detail)
    except Exception:
        pass
    return f"Server error (HTTP {response.status_code}): {response.text[:200]}"


class OverlayServerApi:
    """Talks to the LocalWhisper FastAPI server from worker threads."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)

    def fetch_clients(self) -> list[dict[str, str]]:
        with httpx.Client(timeout=CONNECT_TIMEOUT_S) as client:
            resp = client.get(f"{self.base_url}/api/clients")
            if resp.status_code >= 400:
                return []
            raw = resp.json().get("clients")
            if not isinstance(raw, list):
                return []
            return [
                {"id": str(item.get("id", "")), "name": str(item.get("name", "PC"))}
                for item in raw
                if isinstance(item, dict) and item.get("id")
            ]

    def transcribe_wav(self, wav_bytes: bytes, language: str = "") -> str:
        if len(wav_bytes) <= 44:
            raise ValueError("Recorded audio is empty")
        files = {"audio": ("pc_recording.wav", wav_bytes, "audio/wav")}
        data: dict[str, str] = {}
        if language.strip():
            data["language"] = language.strip()
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(f"{self.base_url}/api/transcribe", files=files, data=data)
            if resp.status_code >= 400:
                raise RuntimeError(_extract_error(resp))
            return str(resp.json().get("text", "") or "")

    def send_keystrokes(self, text: str, client_id: str | None) -> str:
        payload: dict[str, Any] = {"text": text}
        if client_id:
            payload["client_id"] = client_id
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(f"{self.base_url}/api/send", json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(_extract_error(resp))
            return str(resp.json().get("client_name", "PC"))

    def run_agent_stream(
        self,
        prompt: str,
        client_id: str | None,
        session_id: str | None,
        history: list[dict[str, str]],
        interjection: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        on_progress: Callable[[AgentStreamProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> AgentTurnResult:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "origin": "pc_overlay",
            "interjection": bool(interjection),
        }
        if client_id:
            payload["client_id"] = client_id
        if session_id:
            payload["session_id"] = session_id
        if history:
            payload["history"] = history
        if attachments:
            payload["attachments"] = attachments
        return self._consume_ndjson(
            f"{self.base_url}/api/agent/stream",
            payload,
            initial_session_id=session_id or "",
            initial_timeline=[],
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

    def confirm_agent_stream(
        self,
        session_id: str,
        confirmation_id: str,
        approved: bool,
        client_id: str | None,
        initial_timeline: list[TimelineEntry],
        rejection_reason: str | None = None,
        on_progress: Callable[[AgentStreamProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> AgentTurnResult:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "confirmation_id": confirmation_id,
            "approved": bool(approved),
        }
        if client_id:
            payload["client_id"] = client_id
        if not approved and rejection_reason and rejection_reason.strip():
            payload["rejection_reason"] = rejection_reason.strip()
        return self._consume_ndjson(
            f"{self.base_url}/api/agent/confirm/stream",
            payload,
            initial_session_id=session_id,
            initial_timeline=initial_timeline,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

    def _consume_ndjson(
        self,
        url: str,
        payload: dict[str, Any],
        initial_session_id: str,
        initial_timeline: list[TimelineEntry],
        on_progress: Callable[[AgentStreamProgress], None] | None,
        is_cancelled: Callable[[], bool] | None,
    ) -> AgentTurnResult:
        session_id = initial_session_id
        timeline: list[TimelineEntry] = list(initial_timeline)
        reply_parts: list[str] = []
        final_result: AgentTurnResult | None = None

        with httpx.Client(timeout=self._timeout) as client:
            with client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    response.read()
                    raise RuntimeError(_extract_error(response))
                lines_read = 0
                for raw_line in response.iter_lines():
                    if is_cancelled and is_cancelled():
                        raise InterruptedError("Agent stream cancelled")
                    lines_read += 1
                    if lines_read > MAX_STREAM_LINES:
                        break
                    line = (raw_line or "").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev_type = event.get("type")
                    if ev_type == "session":
                        session_id = str(event.get("session_id", session_id))
                    elif ev_type == "thinking_delta":
                        delta = str(event.get("delta", ""))
                        if delta:
                            append_thinking(timeline, delta)
                            if on_progress:
                                on_progress(AgentStreamProgress(session_id, list(timeline), "".join(reply_parts)))
                    elif ev_type == "reply_delta":
                        delta = str(event.get("delta", ""))
                        if delta:
                            reply_parts.append(delta)
                            if on_progress:
                                on_progress(AgentStreamProgress(session_id, list(timeline), "".join(reply_parts)))
                    elif ev_type == "tool_calls":
                        raw_tools = event.get("tool_calls")
                        if isinstance(raw_tools, list):
                            reconcile_tools(timeline, raw_tools)
                            if on_progress:
                                on_progress(AgentStreamProgress(session_id, list(timeline), "".join(reply_parts)))
                    elif ev_type == "done":
                        result_obj = event.get("result")
                        if isinstance(result_obj, dict):
                            final_result = _parse_agent_response(result_obj, timeline)
                    elif ev_type == "error":
                        raise RuntimeError(str(event.get("detail", "Agent stream error")))

        if final_result is None:
            raise RuntimeError("Agent stream ended without a completion result")
        return final_result
