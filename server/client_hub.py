"""WebSocket hub managing connected LocalWhisper PC clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass

from fastapi import WebSocket

LOGGER = logging.getLogger("localwhisper.hub")
DEFAULT_ACK_TIMEOUT_S = 15.0


class NoClientConnectedError(RuntimeError):
    """Raised when no matching PC client is connected."""


class ClientDispatchError(RuntimeError):
    """Raised when a PC client fails to execute a keystroke payload."""


@dataclass
class ConnectedClient:
    client_id: str
    name: str
    websocket: WebSocket
    connected_at: float


@dataclass
class PendingRequest:
    client_id: str
    future: asyncio.Future[tuple[bool, str | None]]


class ClientHub:
    """Tracks PC client WebSockets and dispatches keystroke commands with ACKs."""

    def __init__(self) -> None:
        self._clients: dict[str, ConnectedClient] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket, name: str) -> ConnectedClient:
        clean_name = name.strip()[:64] if name and name.strip() else "PC-Client"
        client_id = uuid.uuid4().hex[:8]
        client = ConnectedClient(
            client_id=client_id,
            name=clean_name,
            websocket=websocket,
            connected_at=time.time(),
        )
        async with self._lock:
            self._clients[client_id] = client
        LOGGER.info("PC client connected: %s [%s]", client.name, client.client_id)
        return client

    async def unregister(self, client_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(client_id, None)
            stale_ids = [
                req_id
                for req_id, req in self._pending.items()
                if req.client_id == client_id
            ]
            for req_id in stale_ids:
                req = self._pending.pop(req_id)
                if not req.future.done():
                    req.future.set_result((False, "PC client disconnected"))
        if client is not None:
            LOGGER.info("PC client disconnected: %s [%s]", client.name, client.client_id)

    async def list_clients(self) -> list[dict[str, object]]:
        async with self._lock:
            return [
                {
                    "id": client.client_id,
                    "name": client.name,
                    "connected_at": client.connected_at,
                }
                for client in self._clients.values()
            ]

    async def resolve_ack(self, request_id: str, ok: bool, error: str | None = None) -> None:
        async with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result((ok, error))

    async def send_keystrokes(
        self,
        text: str,
        client_id: str | None = None,
        timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
    ) -> tuple[str, str]:
        if not text:
            raise ValueError("Keystroke text must not be empty")

        target = await self._select_client(client_id)
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str | None]] = loop.create_future()

        async with self._lock:
            self._pending[request_id] = PendingRequest(client_id=target.client_id, future=future)

        payload = json.dumps({"type": "send", "id": request_id, "text": text})
        try:
            await target.websocket.send_text(payload)
            ok, error = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise ClientDispatchError(
                f"Timed out waiting for PC client '{target.name}' to type keystrokes"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ClientDispatchError):
                raise
            raise ClientDispatchError(
                f"Failed to send payload to PC client '{target.name}': {exc}"
            ) from exc
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

        if not ok:
            raise ClientDispatchError(error or f"PC client '{target.name}' reported an error")

        return target.client_id, target.name

    async def _select_client(self, client_id: str | None) -> ConnectedClient:
        async with self._lock:
            if not self._clients:
                raise NoClientConnectedError(
                    "No PC client is connected. Start pc_client/run_client.bat on your target PC."
                )
            if client_id:
                client = self._clients.get(client_id)
                if client is None:
                    raise NoClientConnectedError(f"PC client '{client_id}' is not connected")
                return client
            # Default to the most recently connected client
            return list(self._clients.values())[-1]
