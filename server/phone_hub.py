"""WebSocket hub for the LocalWhisper phone link.

Protocol on /ws/phone (JSON text frames):
    server -> phone  {"type": "welcome", "phone_id": "...", "name": "..."}
    server -> phone  {"type": "request", "id": "...", "command": "phone.snapshot", "arguments": {...}}
    phone  -> server {"type": "result", "id": "...", "ok": true, "result": {...}, "error": "..."}
    phone  -> server {"type": "event", "event": "remote_stop", "data": {...}}
    server -> phone  one-way messages such as {"type": "file_offer", ...}

The phone never pings. The server's WebSocket ping (every few minutes) detects dead links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("localwhisper.phone_hub")

DEFAULT_PHONE_TIMEOUT_S = 15.0
MIN_PHONE_TIMEOUT_S = 0.05
MAX_PHONE_TIMEOUT_S = 120.0
MAX_COMMAND_CHARS = 64
MAX_EVENT_NAME_CHARS = 64
MAX_EVENT_LISTENERS = 16
MAX_NAME_CHARS = 64
DEFAULT_PHONE_NAME = "Phone"

PhoneEventListener = Callable[[str, dict[str, Any]], Awaitable[None]]


class NoPhoneConnectedError(RuntimeError):
    """Raised when no phone is connected over the phone link."""


class PhoneDispatchError(RuntimeError):
    """Raised when the phone did not answer in time or reported an error."""


@dataclass
class ConnectedPhone:
    phone_id: str
    name: str
    websocket: Any
    connected_at: float


@dataclass
class _PendingPhoneRequest:
    phone_id: str
    future: asyncio.Future[tuple[bool, dict[str, Any] | None, str | None]]


def _parse_json_object(raw_message: Any) -> dict[str, Any] | None:
    if not isinstance(raw_message, str):
        return None
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_name(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:MAX_NAME_CHARS] or DEFAULT_PHONE_NAME


class PhoneHub:
    """Tracks phones connected on /ws/phone and runs commands on the most recently connected one."""

    def __init__(self) -> None:
        self._phones: dict[str, ConnectedPhone] = {}
        self._pending: dict[str, _PendingPhoneRequest] = {}
        self._listeners: list[PhoneEventListener] = []
        self._lock = asyncio.Lock()

    async def register(self, websocket: Any, name: str) -> ConnectedPhone:
        phone = ConnectedPhone(
            phone_id=uuid.uuid4().hex[:8],
            name=_clean_name(name),
            websocket=websocket,
            connected_at=time.time(),
        )
        async with self._lock:
            self._phones[phone.phone_id] = phone
        LOGGER.info("Phone connected: %s [%s]", phone.name, phone.phone_id)
        return phone

    async def unregister(self, phone_id: str) -> None:
        async with self._lock:
            phone = self._phones.pop(phone_id, None)
            stale_ids = [rid for rid, req in self._pending.items() if req.phone_id == phone_id]
            stale = [self._pending.pop(rid) for rid in stale_ids]
        for request in stale:
            if not request.future.done():
                request.future.set_result((False, None, "Phone disconnected"))
        if phone is not None:
            LOGGER.info("Phone disconnected: %s [%s]", phone.name, phone.phone_id)

    async def list_phones(self) -> list[dict[str, object]]:
        async with self._lock:
            return [
                {"id": phone.phone_id, "name": phone.name, "connected_at": phone.connected_at}
                for phone in self._phones.values()
            ]

    def add_event_listener(self, listener: PhoneEventListener) -> None:
        if len(self._listeners) >= MAX_EVENT_LISTENERS:
            raise RuntimeError(f"At most {MAX_EVENT_LISTENERS} phone event listeners are supported")
        self._listeners.append(listener)

    async def call(
        self,
        command: str,
        arguments: dict[str, Any],
        timeout_s: float = DEFAULT_PHONE_TIMEOUT_S,
    ) -> tuple[str, dict[str, Any]]:
        """Runs a command on the newest phone and returns (phone name, result)."""
        clean_command = str(command or "").strip()
        if not clean_command or len(clean_command) > MAX_COMMAND_CHARS:
            raise ValueError(f"command must be 1-{MAX_COMMAND_CHARS} characters")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        bounded_timeout = max(MIN_PHONE_TIMEOUT_S, min(float(timeout_s), MAX_PHONE_TIMEOUT_S))
        target = await self._newest_phone()
        request_id = uuid.uuid4().hex
        future: asyncio.Future[tuple[bool, dict[str, Any] | None, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        async with self._lock:
            self._pending[request_id] = _PendingPhoneRequest(phone_id=target.phone_id, future=future)
        payload = json.dumps(
            {"type": "request", "id": request_id, "command": clean_command, "arguments": arguments}
        )
        try:
            await target.websocket.send_text(payload)
            ok, result, error = await asyncio.wait_for(future, timeout=bounded_timeout)
        except asyncio.TimeoutError as exc:
            raise PhoneDispatchError(
                f"Timed out waiting for the phone '{target.name}' to run '{clean_command}'"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - socket failures become one dispatch error
            raise PhoneDispatchError(f"Could not reach the phone '{target.name}': {exc}") from exc
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)
        if not ok:
            raise PhoneDispatchError(error or f"The phone could not run '{clean_command}'")
        return target.name, (result if result is not None else {"ok": True})

    async def notify(self, payload: dict[str, Any]) -> bool:
        """Sends a one-way message to the newest phone; False when none is connected or sending fails."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        try:
            target = await self._newest_phone()
        except NoPhoneConnectedError:
            return False
        try:
            await target.websocket.send_text(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - a dead socket just means 'not delivered'
            LOGGER.warning("Could not notify phone %s: %s", target.name, exc)
            return False
        return True

    async def resolve_result(
        self,
        request_id: str,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result((ok, result, error))

    async def handle_message(self, phone_id: str, raw_message: str) -> None:
        """Handles one text frame from a phone: a command result or an event."""
        message = _parse_json_object(raw_message)
        if message is None:
            LOGGER.warning("Ignoring a malformed message from phone %s", phone_id)
            return
        msg_type = message.get("type")
        if msg_type == "result":
            await self._handle_result(message)
        elif msg_type == "event":
            await self._handle_event(phone_id, message)

    async def _handle_result(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("id", "") or "")
        if not request_id:
            return
        raw_result = message.get("result")
        error = message.get("error")
        await self.resolve_result(
            request_id,
            bool(message.get("ok", False)),
            raw_result if isinstance(raw_result, dict) else None,
            str(error) if error else None,
        )

    async def _handle_event(self, phone_id: str, message: dict[str, Any]) -> None:
        event = str(message.get("event", "") or "").strip()
        if not event or len(event) > MAX_EVENT_NAME_CHARS:
            return
        raw_data = message.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        for listener in list(self._listeners):
            try:
                await listener(event, data)
            except Exception as exc:  # noqa: BLE001 - one bad listener must not break the link
                LOGGER.warning("Phone event listener failed for '%s' from %s: %s", event, phone_id, exc)

    async def _newest_phone(self) -> ConnectedPhone:
        async with self._lock:
            if not self._phones:
                raise NoPhoneConnectedError(
                    "No phone is connected to the phone link. Open LocalWhisper on the phone so it can connect."
                )
            return list(self._phones.values())[-1]
