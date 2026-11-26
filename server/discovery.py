"""UDP discovery responder: lets the LocalWhisper Android app find this server on the LAN.

The app broadcasts DISCOVERY_MAGIC to UDP port 8766. The server answers the sender directly
with a small JSON object naming itself and its HTTP port. Any other packet is ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("localwhisper.discovery")

DISCOVERY_MAGIC = b"LOCALWHISPER_DISCOVER/1"
DEFAULT_DISCOVERY_PORT = 8766
MAX_REQUEST_BYTES = 64
SERVICE_NAME = "localwhisper"
PROTOCOL_VERSION = 1
MAX_NAME_CHARS = 64
MAX_BACKEND_CHARS = 16
FALLBACK_NAME = "LocalWhisper"
MIN_REPLY_INTERVAL_S = 0.3
MAX_TRACKED_SENDERS = 256


def clean_server_name(raw: Any) -> str:
    clean = " ".join(str(raw or "").split())[:MAX_NAME_CHARS]
    return clean or FALLBACK_NAME


def default_server_name() -> str:
    try:
        return clean_server_name(socket.gethostname())
    except OSError:
        return FALLBACK_NAME


def build_discovery_reply(data: Any, name: str, http_port: Any, asr_backend: str = "") -> bytes | None:
    """JSON reply for a valid discovery request, or None when the packet must be ignored."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_REQUEST_BYTES:
        return None
    if bytes(data).strip() != DISCOVERY_MAGIC:
        return None
    try:
        port = int(http_port)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    payload = {
        "service": SERVICE_NAME,
        "version": PROTOCOL_VERSION,
        "name": clean_server_name(name),
        "port": port,
        "asr_backend": str(asr_backend or "")[:MAX_BACKEND_CHARS],
    }
    return json.dumps(payload).encode("utf-8")


class SenderRateLimiter:
    """Answers each sender at most once per interval; the table of senders stays bounded."""

    def __init__(
        self,
        min_interval_s: float = MIN_REPLY_INTERVAL_S,
        max_senders: int = MAX_TRACKED_SENDERS,
    ) -> None:
        if max_senders < 1:
            raise ValueError("max_senders must be at least 1")
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.max_senders = max_senders
        self._last_reply: dict[str, float] = {}

    def allow(self, sender: str, now: float) -> bool:
        last = self._last_reply.get(sender)
        if last is not None and now - last < self.min_interval_s:
            return False
        if sender not in self._last_reply and len(self._last_reply) >= self.max_senders:
            self._last_reply.clear()
        self._last_reply[sender] = now
        return True


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Replies to discovery broadcasts with this server's name and HTTP port."""

    def __init__(self, name: str, http_port: int, backend_getter: Callable[[], str]) -> None:
        self._name = clean_server_name(name)
        self._http_port = http_port
        self._backend_getter = backend_getter
        self._limiter = SenderRateLimiter()
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        if self._transport is None or not addr:
            return
        reply = build_discovery_reply(data, self._name, self._http_port, self._current_backend())
        if reply is None:
            return
        sender = str(addr[0])
        if not self._limiter.allow(sender, time.monotonic()):
            return
        try:
            self._transport.sendto(reply, addr)
        except OSError as exc:
            LOGGER.warning("Could not answer discovery from %s: %s", sender, exc)

    def error_received(self, exc: Exception) -> None:
        LOGGER.warning("Discovery socket error: %s", exc)

    def _current_backend(self) -> str:
        try:
            return str(self._backend_getter() or "")
        except Exception as exc:  # noqa: BLE001 - discovery must never break the server
            LOGGER.warning("Could not read the ASR backend for discovery: %s", exc)
            return ""


async def start_discovery_responder(
    port: int,
    name: str,
    http_port: int,
    backend_getter: Callable[[], str],
) -> asyncio.DatagramTransport | None:
    """Listens for discovery broadcasts on UDP `port` (0 = any free port). None when it cannot bind."""
    try:
        bind_port = int(port)
    except (TypeError, ValueError):
        LOGGER.warning("LAN discovery disabled: invalid port %r", port)
        return None
    if not 0 <= bind_port <= 65535:
        LOGGER.warning("LAN discovery disabled: port %d is out of range", bind_port)
        return None
    loop = asyncio.get_running_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(name, http_port, backend_getter),
            local_addr=("0.0.0.0", bind_port),
            allow_broadcast=True,
        )
    except OSError as exc:
        LOGGER.warning("LAN discovery disabled: could not bind UDP port %d (%s)", bind_port, exc)
        return None
    LOGGER.info("LAN discovery listening on UDP %d as '%s'", bind_port, clean_server_name(name))
    return transport
