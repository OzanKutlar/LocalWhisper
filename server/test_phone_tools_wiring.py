"""Tests for Batch 4 phone tools (control_phone, phone_communication, send_phone_file_to_pc)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_engine import AGENT_TOOLS, AgentEngine, check_confirmation_required  # noqa: E402
from client_hub import ClientHub  # noqa: E402
from phone_hub import PhoneHub  # noqa: E402


class FakePhoneHub(PhoneHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        command: str,
        arguments: dict[str, Any],
        timeout_s: float = 15.0,
    ) -> tuple[str, dict[str, Any]]:
        self.calls.append((command, arguments))
        return "Pixel", {"ok": True, "command": command}


class ScriptedEngine(AgentEngine):
    def __init__(self, phone_hub: PhoneHub, scripted: list[dict[str, Any]]) -> None:
        super().__init__(ClientHub(), "http://localhost:1/v1", "test", phone_hub=phone_hub)
        self._scripted = list(scripted)

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._scripted.pop(0) if self._scripted else {"role": "assistant", "content": "Done."}


def tool_msg(name: str, args_json: str, call_id: str = "c1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args_json}}],
    }


class PhoneToolsWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_phone_tools_are_registered_and_send_file_is_gated(self) -> None:
        names = {t["function"]["name"] for t in AGENT_TOOLS}
        self.assertIn("control_phone", names)
        self.assertIn("phone_communication", names)
        self.assertIn("send_phone_file_to_pc", names)

        self.assertFalse(check_confirmation_required("control_phone", {"action": "set_flashlight", "enabled": True})[0])
        self.assertFalse(check_confirmation_required("phone_communication", {"action": "draft_sms", "recipient": "Mom"})[0])
        needs, desc = check_confirmation_required("send_phone_file_to_pc", {"which": "latest_screenshot"})
        self.assertTrue(needs)
        self.assertIn("latest screenshot", desc)

    async def test_routes_control_phone_and_communication_over_phone_link(self) -> None:
        hub = FakePhoneHub()
        scripted = [
            tool_msg("control_phone", '{"action": "set_flashlight", "enabled": true}', "c1"),
            tool_msg("phone_communication", '{"action": "draft_sms", "recipient": "Mom", "message": "On my way"}', "c2"),
            {"role": "assistant", "content": "Flashlight on and SMS draft opened."},
        ]
        engine = ScriptedEngine(hub, scripted)
        res = await engine.run_turn("Turn on flashlight and text Mom")
        self.assertEqual(res.status, "completed")
        self.assertEqual([c for c, _a in hub.calls], ["phone.control", "phone.communication"])

    async def test_send_phone_file_gates_then_dispatches_to_phone(self) -> None:
        hub = FakePhoneHub()
        scripted = [
            tool_msg("send_phone_file_to_pc", '{"which": "latest_screenshot"}', "c1"),
            {"role": "assistant", "content": "Sent your latest screenshot to the PC."},
        ]
        engine = ScriptedEngine(hub, scripted)
        first = await engine.run_turn("Send my latest screenshot to my PC")
        self.assertEqual(first.status, "confirmation_required")
        self.assertEqual(hub.calls, [])

        resumed = await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=True,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(hub.calls[0][0], "phone.send_file")


if __name__ == "__main__":
    unittest.main()
