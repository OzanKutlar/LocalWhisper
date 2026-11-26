"""Unit tests for the one-way messages in server/client_hub.py."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client_hub import ClientHub  # noqa: E402


class FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise ConnectionError("socket closed")
        self.sent.append(json.loads(text))


class ClientHubNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_without_clients_is_not_delivered(self) -> None:
        self.assertFalse(await ClientHub().notify({"type": "file_offer"}))

    async def test_notify_falls_back_to_the_newest_client(self) -> None:
        hub = ClientHub()
        socket = FakeSocket()
        await hub.register(socket, "Desk")
        self.assertTrue(await hub.notify({"type": "file_offer", "id": "abc"}, client_id="gone"))
        self.assertEqual(socket.sent[-1]["id"], "abc")

    async def test_notify_reports_a_send_failure(self) -> None:
        hub = ClientHub()
        await hub.register(FakeSocket(fail=True), "Desk")
        self.assertFalse(await hub.notify({"type": "file_offer"}))

    async def test_broadcast_counts_deliveries(self) -> None:
        hub = ClientHub()
        good = FakeSocket()
        await hub.register(good, "Desk")
        await hub.register(FakeSocket(fail=True), "Laptop")
        self.assertEqual(await hub.broadcast({"type": "notify", "title": "Hi"}), 1)
        self.assertEqual(good.sent[-1]["title"], "Hi")


if __name__ == "__main__":
    unittest.main()
