"""Unit tests for server/discovery.py."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discovery import (  # noqa: E402
    DISCOVERY_MAGIC,
    FALLBACK_NAME,
    MAX_REQUEST_BYTES,
    SenderRateLimiter,
    build_discovery_reply,
    start_discovery_responder,
)


class ReplyTests(unittest.TestCase):
    def test_magic_gets_json_reply(self) -> None:
        reply = build_discovery_reply(DISCOVERY_MAGIC, "GamingPC", 8765, "llm")
        data = json.loads(reply)
        self.assertEqual(data["service"], "localwhisper")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["name"], "GamingPC")
        self.assertEqual(data["port"], 8765)
        self.assertEqual(data["asr_backend"], "llm")

    def test_trailing_whitespace_is_tolerated(self) -> None:
        self.assertIsNotNone(build_discovery_reply(DISCOVERY_MAGIC + b"\n", "PC", 8765))

    def test_other_packets_are_ignored(self) -> None:
        self.assertIsNone(build_discovery_reply(b"hello", "PC", 8765))
        self.assertIsNone(build_discovery_reply(b"", "PC", 8765))
        self.assertIsNone(build_discovery_reply(DISCOVERY_MAGIC + b"x" * MAX_REQUEST_BYTES, "PC", 8765))
        self.assertIsNone(build_discovery_reply("LOCALWHISPER_DISCOVER/1", "PC", 8765))

    def test_invalid_http_port_is_ignored(self) -> None:
        self.assertIsNone(build_discovery_reply(DISCOVERY_MAGIC, "PC", 0))
        self.assertIsNone(build_discovery_reply(DISCOVERY_MAGIC, "PC", 70000))
        self.assertIsNone(build_discovery_reply(DISCOVERY_MAGIC, "PC", "abc"))

    def test_blank_name_falls_back(self) -> None:
        data = json.loads(build_discovery_reply(DISCOVERY_MAGIC, "   ", 8765))
        self.assertEqual(data["name"], FALLBACK_NAME)


class RateLimiterTests(unittest.TestCase):
    def test_limits_each_sender_and_stays_bounded(self) -> None:
        limiter = SenderRateLimiter(min_interval_s=1.0, max_senders=2)
        self.assertTrue(limiter.allow("a", 10.0))
        self.assertFalse(limiter.allow("a", 10.5))
        self.assertTrue(limiter.allow("a", 11.1))
        self.assertTrue(limiter.allow("b", 11.1))
        self.assertTrue(limiter.allow("c", 11.1))


def _ask(port: int) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2.0)
        sock.sendto(DISCOVERY_MAGIC, ("127.0.0.1", port))
        data, _addr = sock.recvfrom(2048)
        return data


class ResponderTests(unittest.IsolatedAsyncioTestCase):
    async def test_responder_answers_over_udp(self) -> None:
        transport = await start_discovery_responder(
            port=0,
            name="TestPC",
            http_port=8765,
            backend_getter=lambda: "local",
        )
        self.assertIsNotNone(transport)
        try:
            port = transport.get_extra_info("sockname")[1]
            reply = await asyncio.to_thread(_ask, port)
        finally:
            transport.close()
        data = json.loads(reply)
        self.assertEqual(data["name"], "TestPC")
        self.assertEqual(data["asr_backend"], "local")


if __name__ == "__main__":
    unittest.main()
