"""Unit tests for server/phone_hub.py."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phone_hub import NoPhoneConnectedError, PhoneDispatchError, PhoneHub  # noqa: E402

MAX_WAIT_LOOPS = 100


class FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise ConnectionError("socket closed")
        self.sent.append(json.loads(text))


async def wait_for_request(socket: FakeSocket) -> dict[str, Any]:
    for _ in range(MAX_WAIT_LOOPS):
        if socket.sent:
            return socket.sent[-1]
        await asyncio.sleep(0)
    raise AssertionError("the hub never sent a request")


def result_frame(request_id: str, ok: bool, result: dict[str, Any] | None = None, error: str = "") -> str:
    return json.dumps({"type": "result", "id": request_id, "ok": ok, "result": result, "error": error})


class PhoneHubCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_returns_the_phone_result(self) -> None:
        hub = PhoneHub()
        socket = FakeSocket()
        phone = await hub.register(socket, "  Pixel   8 ")
        self.assertEqual(phone.name, "Pixel 8")
        task = asyncio.create_task(hub.call("phone.snapshot", {"a": 1}, timeout_s=2))
        request = await wait_for_request(socket)
        self.assertEqual(
            (request["type"], request["command"], request["arguments"]),
            ("request", "phone.snapshot", {"a": 1}),
        )
        await hub.handle_message(
            phone.phone_id, result_frame(request["id"], True, {"phone_screen": {"app_name": "Gmail"}})
        )
        name, result = await task
        self.assertEqual(name, "Pixel 8")
        self.assertEqual(result["phone_screen"]["app_name"], "Gmail")

    async def test_error_result_raises(self) -> None:
        hub = PhoneHub()
        socket = FakeSocket()
        phone = await hub.register(socket, "Pixel")
        task = asyncio.create_task(hub.call("phone.ui_action", {}, timeout_s=2))
        request = await wait_for_request(socket)
        await hub.handle_message(phone.phone_id, result_frame(request["id"], False, error="phone_locked"))
        with self.assertRaises(PhoneDispatchError) as ctx:
            await task
        self.assertIn("phone_locked", str(ctx.exception))

    async def test_no_phone_raises(self) -> None:
        with self.assertRaises(NoPhoneConnectedError):
            await PhoneHub().call("phone.snapshot", {})

    async def test_timeout_raises(self) -> None:
        hub = PhoneHub()
        await hub.register(FakeSocket(), "Pixel")
        with self.assertRaises(PhoneDispatchError):
            await hub.call("phone.snapshot", {}, timeout_s=0.05)

    async def test_disconnect_fails_the_pending_call(self) -> None:
        hub = PhoneHub()
        socket = FakeSocket()
        phone = await hub.register(socket, "Pixel")
        task = asyncio.create_task(hub.call("phone.snapshot", {}, timeout_s=2))
        await wait_for_request(socket)
        await hub.unregister(phone.phone_id)
        with self.assertRaises(PhoneDispatchError) as ctx:
            await task
        self.assertIn("disconnected", str(ctx.exception))

    async def test_send_failure_raises_dispatch_error(self) -> None:
        hub = PhoneHub()
        await hub.register(FakeSocket(fail=True), "Pixel")
        with self.assertRaises(PhoneDispatchError):
            await hub.call("phone.snapshot", {}, timeout_s=1)

    async def test_bad_arguments_are_rejected(self) -> None:
        hub = PhoneHub()
        await hub.register(FakeSocket(), "Pixel")
        with self.assertRaises(ValueError):
            await hub.call("", {})
        with self.assertRaises(ValueError):
            await hub.call("phone.snapshot", [])  # type: ignore[arg-type]


class PhoneHubEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_reach_every_listener_even_if_one_fails(self) -> None:
        hub = PhoneHub()
        phone = await hub.register(FakeSocket(), "Pixel")
        seen: list[tuple[str, dict[str, Any]]] = []

        async def broken(_event: str, _data: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        async def recorder(event: str, data: dict[str, Any]) -> None:
            seen.append((event, data))

        hub.add_event_listener(broken)
        hub.add_event_listener(recorder)
        await hub.handle_message(
            phone.phone_id, json.dumps({"type": "event", "event": "remote_stop", "data": {"x": 1}})
        )
        await hub.handle_message(phone.phone_id, "not json")
        self.assertEqual(seen, [("remote_stop", {"x": 1})])

    async def test_notify(self) -> None:
        hub = PhoneHub()
        self.assertFalse(await hub.notify({"type": "file_offer"}))
        socket = FakeSocket()
        await hub.register(socket, "Pixel")
        self.assertTrue(await hub.notify({"type": "file_offer", "id": "abc"}))
        self.assertEqual(socket.sent[-1]["id"], "abc")

    async def test_list_phones(self) -> None:
        hub = PhoneHub()
        phone = await hub.register(FakeSocket(), "")
        phones = await hub.list_phones()
        self.assertEqual([(p["id"], p["name"]) for p in phones], [(phone.phone_id, "Phone")])
        await hub.unregister(phone.phone_id)
        self.assertEqual(await hub.list_phones(), [])


if __name__ == "__main__":
    unittest.main()
