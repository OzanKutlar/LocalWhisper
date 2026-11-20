"""Thinking-enabled tool-calling agent engine for LocalWhisper."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from client_hub import ClientDispatchError, ClientHub, NoClientConnectedError

LOGGER = logging.getLogger("localwhisper.agent")

MAX_TOOL_ITERATIONS = 6
MAX_SESSIONS = 50
MAX_SESSION_MESSAGES = 36
MAX_MACRO_STEPS = 25
LLM_TIMEOUT_S = 75.0

THINK_TAG_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
DESTRUCTIVE_AHK_PATTERN = re.compile(
    r"\{(?:!F4|\^w|\^q|#r|#x|Delete|Alt\s+down)\}",
    re.IGNORECASE,
)
SAFE_APP_ALIASES = frozenset(
    {
        "notepad",
        "calculator",
        "calc",
        "explorer",
        "mspaint",
        "paint",
        "code",
        "vscode",
        "visual studio code",
        "chrome",
        "google chrome",
        "msedge",
        "edge",
        "firefox",
        "spotify",
        "discord",
        "slack",
        "obs",
        "vlc",
        "taskmgr",
        "wt",
        "terminal",
    }
)

SYSTEM_PROMPT = (
    "You are the LocalWhisper Agent, a voice-activated desktop and phone assistant. "
    "Think step-by-step about the user's request before calling tools.\n"
    "You have tools to control the user's Windows PC (via AutoHotkey macros, window/app "
    "management, media keys, and opening URLs) AND tools to trigger native intents on "
    "the user's Android phone (alarms, timers, web search, opening URLs, launching apps).\n"
    "Guidelines:\n"
    "1. For multi-step typing, shortcuts, or editing on the PC, use `execute_pc_ahk_sequence`. "
    "Inside `text`, use AutoHotkey brace tokens for special keys: {Enter}, {Tab}, {Backspace}, "
    "{Esc}, {Up}, {Down}, {Left}, {Right}, {^a} (Ctrl+A), {^c} (Ctrl+C), {^v} (Ctrl+V), "
    "{^s} (Ctrl+S), {^z} (Ctrl+Z), {^f} (Ctrl+F), {^+t}, etc. Everything outside braces is "
    "typed literally.\n"
    "2. For pausing music, skipping tracks, or changing PC volume, use `control_pc_media`.\n"
    "3. For switching windows, launching PC apps, closing windows, or locking the PC, "
    "use `manage_pc_app`. For opening websites on the PC, use `open_pc_url`.\n"
    "4. For setting alarms, timers, searching the web on the phone, or opening phone apps, "
    "use `trigger_android_intent`.\n"
    "5. Keep your final spoken/written reply concise and helpful."
)

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_pc_ahk_sequence",
            "description": (
                "Execute a multi-step AutoHotkey v1.1 macro sequence on the target Windows PC. "
                "Each step can type literal text or trigger AHK key tokens in braces like "
                "{Enter}, {Tab}, {Backspace}, {^a}, {^c}, {^v}, {^s}, followed by an optional "
                "wait_ms pause."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Ordered list of AHK keystroke steps to run on the PC.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "Literal text and/or AHK brace tokens (e.g. '{^a}{Backspace}SELECT * FROM users;{Enter}')."
                                    ),
                                },
                                "wait_ms": {
                                    "type": "integer",
                                    "description": "Pause in milliseconds after this step (0 to 5000).",
                                    "default": 150,
                                },
                            },
                            "required": ["text"],
                        },
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": "Set true if this sequence performs a destructive action (e.g. deleting data or closing apps).",
                        "default": False,
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_pc_app",
            "description": (
                "Launch an application, focus/switch to an existing window, close a window, "
                "or lock the workstation on the target Windows PC."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["launch_app", "focus_window", "close_window", "lock_pc"],
                        "description": "The application or window action to perform.",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "App name or window title (e.g. 'notepad', 'chrome', 'Spotify', 'Visual Studio Code'). "
                            "Not required for 'lock_pc'."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_pc_media",
            "description": "Control media playback or system volume on the target Windows PC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "play_pause",
                            "next_track",
                            "prev_track",
                            "stop",
                            "volume_up",
                            "volume_down",
                            "mute",
                        ],
                        "description": "Media or volume control command.",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Number of increments for volume_up or volume_down (1 to 20).",
                        "default": 2,
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_pc_url",
            "description": "Open an HTTP or HTTPS URL in the default browser on the target Windows PC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full HTTP/HTTPS URL to open on the PC.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_android_intent",
            "description": (
                "Trigger a native system action directly on the user's Android phone: "
                "set an alarm, set a countdown timer, open a URL on the phone, search the web "
                "on the phone, or launch an installed Android application."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent_type": {
                        "type": "string",
                        "enum": [
                            "set_alarm",
                            "set_timer",
                            "open_url",
                            "web_search",
                            "launch_app",
                        ],
                        "description": "The type of Android intent to execute on the phone.",
                    },
                    "label": {
                        "type": "string",
                        "description": (
                            "Alarm/timer label, or the Android app name/package to open for 'launch_app'."
                        ),
                    },
                    "hour": {
                        "type": "integer",
                        "description": "Alarm hour in 24-hour format (0-23) for 'set_alarm'.",
                    },
                    "minute": {
                        "type": "integer",
                        "description": "Alarm minute (0-59) for 'set_alarm'.",
                    },
                    "seconds": {
                        "type": "integer",
                        "description": "Timer duration in seconds for 'set_timer'.",
                    },
                    "url_or_query": {
                        "type": "string",
                        "description": "URL for 'open_url' or search text for 'web_search'.",
                    },
                },
                "required": ["intent_type"],
            },
        },
    },
]


@dataclass
class ToolCallRecord:
    tool: str
    status: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {"tool": self.tool, "status": self.status, "summary": self.summary}


@dataclass
class PendingConfirmation:
    confirmation_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    description: str
    remaining_tool_calls: list[dict[str, Any]]
    client_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "tool_name": self.tool_name,
            "description": self.description,
            "arguments": self.arguments,
        }


@dataclass
class AgentSession:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    tool_records: list[ToolCallRecord] = field(default_factory=list)
    android_actions: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: PendingConfirmation | None = None
    updated_at: float = field(default_factory=time.time)


@dataclass
class AgentTurnResult:
    status: str
    session_id: str
    thinking: str
    reply: str
    tool_calls: list[dict[str, str]]
    android_actions: list[dict[str, Any]]
    pending_confirmation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "thinking": self.thinking,
            "reply": self.reply,
            "tool_calls": self.tool_calls,
            "android_actions": self.android_actions,
            "pending_confirmation": self.pending_confirmation,
        }


def extract_thinking_and_content(message: dict[str, Any]) -> tuple[str, str]:
    """Extracts thinking trace and clean response content from an LLM message."""
    thinking_parts: list[str] = []
    for key in ("reasoning_content", "thinking", "reasoning"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            thinking_parts.append(val.strip())

    raw_content = str(message.get("content") or "")
    for match in THINK_TAG_PATTERN.finditer(raw_content):
        thought = match.group(1).strip()
        if thought:
            thinking_parts.append(thought)

    clean_content = THINK_TAG_PATTERN.sub("", raw_content).strip()
    return "\n\n".join(thinking_parts), clean_content


def check_confirmation_required(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, str]:
    """Determines whether a tool call requires explicit user approval on the phone."""
    if tool_name == "manage_pc_app":
        action = str(arguments.get("action", "")).strip()
        target = str(arguments.get("target", "")).strip()
        if action == "close_window":
            return True, f"Close window '{target or 'active window'}' on your PC?"
        if action == "lock_pc":
            return True, "Lock your Windows PC workstation?"
        if action == "launch_app":
            normalized = target.lower().removesuffix(".exe")
            if normalized not in SAFE_APP_ALIASES:
                return True, f"Launch application '{target}' on your PC?"

    if tool_name == "execute_pc_ahk_sequence":
        steps = arguments.get("steps")
        if not isinstance(steps, list):
            return False, ""
        combined = " ".join(str(s.get("text", "")) for s in steps if isinstance(s, dict))
        flagged = bool(arguments.get("requires_confirmation", False))
        has_destructive_key = DESTRUCTIVE_AHK_PATTERN.search(combined) is not None
        if flagged or has_destructive_key:
            preview = combined[:80] + ("…" if len(combined) > 80 else "")
            return True, f"Run {len(steps)}-step AHK macro on PC: {preview}"

    return False, ""


def summarize_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """Builds a short human-readable label for the Android tool status pills."""
    if tool_name == "execute_pc_ahk_sequence":
        steps = arguments.get("steps")
        count = len(steps) if isinstance(steps, list) else 0
        return f"AHK macro ({count} {'step' if count == 1 else 'steps'})"
    if tool_name == "manage_pc_app":
        action = str(arguments.get("action", "app"))
        target = str(arguments.get("target", "")).strip()
        return f"{action}: {target}" if target else action
    if tool_name == "control_pc_media":
        return f"Media: {arguments.get('action', '')}"
    if tool_name == "open_pc_url":
        return f"Open URL: {arguments.get('url', '')}"
    if tool_name == "trigger_android_intent":
        intent_type = str(arguments.get("intent_type", "intent"))
        detail = (
            arguments.get("label")
            or arguments.get("url_or_query")
            or (f"{arguments.get('seconds')}s" if arguments.get("seconds") else "")
        )
        return f"Phone {intent_type}: {detail}" if detail else f"Phone {intent_type}"
    return tool_name


class AgentEngine:
    """Manages multi-turn thinking LLM sessions, tool execution, and confirmation gates."""

    def __init__(
        self,
        client_hub: ClientHub,
        llm_base_url: str,
        llm_model: str,
        llm_api_key: str = "local",
        enable_thinking: bool = True,
    ) -> None:
        self.client_hub = client_hub
        self.llm_base_url = llm_base_url.rstrip("/")
        self.llm_model = llm_model.strip()
        self.llm_api_key = llm_api_key.strip() or "local"
        self.enable_thinking = enable_thinking
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    async def run_turn(
        self,
        prompt: str,
        client_id: str | None = None,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AgentTurnResult:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Agent prompt must not be empty")

        session = await self._get_or_create_session(session_id, history)
        session.thinking_chunks.clear()
        session.tool_records.clear()
        session.android_actions.clear()
        session.pending_confirmation = None
        session.messages.append({"role": "user", "content": clean_prompt})
        self._trim_session_messages(session)

        return await self._continue_agent_loop(session, client_id)

    async def confirm_tool(
        self,
        session_id: str,
        confirmation_id: str,
        approved: bool,
        client_id: str | None = None,
    ) -> AgentTurnResult:
        session = await self._get_existing_session(session_id)
        pending = session.pending_confirmation
        if pending is None or pending.confirmation_id != confirmation_id:
            raise ValueError("No matching pending tool confirmation found for this session")

        session.pending_confirmation = None
        effective_client_id = client_id or pending.client_id

        if approved:
            tool_output = await self._execute_single_tool(
                session,
                pending.tool_name,
                pending.arguments,
                effective_client_id,
            )
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending.tool_call_id,
                    "name": pending.tool_name,
                    "content": json.dumps(tool_output),
                }
            )
            gate_hit = await self._process_tool_calls_batch(
                session,
                pending.remaining_tool_calls,
                effective_client_id,
            )
            if gate_hit is not None:
                return gate_hit
        else:
            summary = summarize_tool_call(pending.tool_name, pending.arguments)
            session.tool_records.append(
                ToolCallRecord(tool=pending.tool_name, status="rejected", summary=f"Rejected: {summary}")
            )
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending.tool_call_id,
                    "name": pending.tool_name,
                    "content": json.dumps(
                        {"ok": False, "rejected_by_user": True, "error": "User rejected the action."}
                    ),
                }
            )
            for skipped in pending.remaining_tool_calls:
                skipped_id = str(skipped.get("id") or uuid.uuid4().hex[:8])
                skipped_fn = skipped.get("function") or {}
                skipped_name = str(skipped_fn.get("name") or "unknown")
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": skipped_id,
                        "name": skipped_name,
                        "content": json.dumps({"ok": False, "skipped": True}),
                    }
                )

        return await self._continue_agent_loop(session, effective_client_id)

    async def _continue_agent_loop(
        self,
        session: AgentSession,
        client_id: str | None,
    ) -> AgentTurnResult:
        last_reply = ""
        for _iteration in range(MAX_TOOL_ITERATIONS):
            assistant_msg = await self._request_chat_completion(session.messages)
            thought, clean_content = extract_thinking_and_content(assistant_msg)
            if thought:
                session.thinking_chunks.append(thought)
            if clean_content:
                last_reply = clean_content

            tool_calls = assistant_msg.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                session.messages.append({"role": "assistant", "content": clean_content})
                return self._build_result(session, status="completed", reply=last_reply or "Done.")

            session.messages.append(
                {
                    "role": "assistant",
                    "content": clean_content or "",
                    "tool_calls": tool_calls,
                }
            )
            gate_result = await self._process_tool_calls_batch(session, tool_calls, client_id)
            if gate_result is not None:
                return gate_result

        return self._build_result(
            session,
            status="completed",
            reply=last_reply or "Completed maximum tool steps.",
        )

    async def _process_tool_calls_batch(
        self,
        session: AgentSession,
        tool_calls: list[dict[str, Any]],
        client_id: str | None,
    ) -> AgentTurnResult | None:
        for index, tc in enumerate(tool_calls):
            tc_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")
            fn_obj = tc.get("function") or {}
            tool_name = str(fn_obj.get("name") or "").strip()
            raw_args = fn_obj.get("arguments")
            arguments = self._parse_arguments(raw_args)

            needs_confirm, confirm_desc = check_confirmation_required(tool_name, arguments)
            if needs_confirm:
                confirmation = PendingConfirmation(
                    confirmation_id=uuid.uuid4().hex[:12],
                    tool_call_id=tc_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    description=confirm_desc,
                    remaining_tool_calls=tool_calls[index + 1 :],
                    client_id=client_id,
                )
                session.pending_confirmation = confirmation
                session.tool_records.append(
                    ToolCallRecord(
                        tool=tool_name,
                        status="pending_confirmation",
                        summary=f"Waiting approval: {summarize_tool_call(tool_name, arguments)}",
                    )
                )
                return self._build_result(
                    session,
                    status="confirmation_required",
                    reply=confirm_desc,
                )

            tool_output = await self._execute_single_tool(session, tool_name, arguments, client_id)
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tool_name,
                    "content": json.dumps(tool_output),
                }
            )
        return None

    async def _execute_single_tool(
        self,
        session: AgentSession,
        tool_name: str,
        arguments: dict[str, Any],
        client_id: str | None,
    ) -> dict[str, Any]:
        summary = summarize_tool_call(tool_name, arguments)
        if tool_name == "trigger_android_intent":
            action_payload = {
                "intent_type": str(arguments.get("intent_type", "")),
                "label": str(arguments.get("label", "")),
                "hour": int(arguments.get("hour", -1)) if arguments.get("hour") is not None else -1,
                "minute": int(arguments.get("minute", -1)) if arguments.get("minute") is not None else -1,
                "seconds": int(arguments.get("seconds", 0)) if arguments.get("seconds") is not None else 0,
                "url_or_query": str(arguments.get("url_or_query", "")),
            }
            session.android_actions.append(action_payload)
            session.tool_records.append(
                ToolCallRecord(tool=tool_name, status="queued_on_phone", summary=summary)
            )
            return {"ok": True, "dispatched_to": "android_phone", "action": action_payload}

        try:
            _cid, cname, result = await self.client_hub.call_tool(
                tool_name=tool_name,
                arguments=arguments,
                client_id=client_id,
            )
            session.tool_records.append(
                ToolCallRecord(tool=tool_name, status="ok", summary=f"{summary} ({cname})")
            )
            return {"ok": True, "client": cname, "result": result}
        except (NoClientConnectedError, ClientDispatchError, ValueError) as exc:
            LOGGER.warning("Tool '%s' failed: %s", tool_name, exc)
            session.tool_records.append(
                ToolCallRecord(tool=tool_name, status="error", summary=f"{summary} (failed)")
            )
            return {"ok": False, "error": str(exc)}

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{self.llm_base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.llm_api_key}",
        }
        base_payload: dict[str, Any] = {
            "model": self.llm_model,
            "messages": messages,
            "tools": AGENT_TOOLS,
            "tool_choice": "auto",
        }
        payload_with_thinking = dict(base_payload)
        if self.enable_thinking:
            payload_with_thinking["chat_template_kwargs"] = {"enable_thinking": True}

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as client:
            try:
                response = await client.post(url, headers=headers, json=payload_with_thinking)
                if response.status_code == 400 and self.enable_thinking:
                    response = await client.post(url, headers=headers, json=base_payload)
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"Could not reach thinking LLM at {url}. "
                    f"Ensure your local LLM server (e.g. Ollama / vLLM) is running: {exc}"
                ) from exc

        if response.status_code >= 400:
            detail = response.text[:300]
            raise RuntimeError(f"LLM server returned HTTP {response.status_code}: {detail}")

        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM server returned an empty choices list")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM server response missing message object")
        return message

    def _build_result(
        self,
        session: AgentSession,
        status: str,
        reply: str,
    ) -> AgentTurnResult:
        thinking_text = "\n\n".join(session.thinking_chunks)
        pending_dict = (
            session.pending_confirmation.to_dict()
            if session.pending_confirmation is not None
            else None
        )
        return AgentTurnResult(
            status=status,
            session_id=session.session_id,
            thinking=thinking_text,
            reply=reply,
            tool_calls=[record.to_dict() for record in session.tool_records],
            android_actions=list(session.android_actions),
            pending_confirmation=pending_dict,
        )

    async def _get_or_create_session(
        self,
        session_id: str | None,
        history: list[dict[str, str]] | None = None,
    ) -> AgentSession:
        async with self._lock:
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.updated_at = time.time()
                return session

            if len(self._sessions) >= MAX_SESSIONS:
                oldest_id = min(self._sessions, key=lambda sid: self._sessions[sid].updated_at)
                self._sessions.pop(oldest_id, None)

            new_id = session_id.strip() if session_id and session_id.strip() else uuid.uuid4().hex[:12]
            initial_messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
            if isinstance(history, list):
                for item in history[-(MAX_SESSION_MESSAGES - 2) :]:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role", "")).strip()
                    content = str(item.get("content", "")).strip()
                    if role in ("user", "assistant") and content:
                        initial_messages.append({"role": role, "content": content})

            session = AgentSession(
                session_id=new_id,
                messages=initial_messages,
            )
            self._sessions[new_id] = session
            return session

    async def _get_existing_session(self, session_id: str) -> AgentSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Session '{session_id}' not found or expired")
            session.updated_at = time.time()
            return session

    @staticmethod
    def _trim_session_messages(session: AgentSession) -> None:
        if len(session.messages) <= MAX_SESSION_MESSAGES:
            return
        system_msg = session.messages[0]
        recent = session.messages[-(MAX_SESSION_MESSAGES - 1) :]
        session.messages = [system_msg] + recent

    @staticmethod
    def _parse_arguments(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                LOGGER.warning("Failed to decode tool JSON arguments: %s", raw_args[:120])
        return {}
