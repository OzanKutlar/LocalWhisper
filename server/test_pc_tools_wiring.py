"""Tests for Batch 2 server tool schemas, confirmation gates and clipboard routing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_engine import (  # noqa: E402
    AGENT_TOOLS,
    AgentEngine,
    check_confirmation_required,
    summarize_tool_call,
)
from client_hub import ClientHub  # noqa: E402
from phone_hub import PhoneHub  # noqa: E402


class RecordingClientHub(ClientHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        client_id: str | None = None,
        timeout_s: float = 30.0,
    ) -> tuple[str, str, dict[str, object]]:
        self.calls.append((tool_name, arguments))
        if tool_name == "read_pc_clipboard":
            return "pc1", "Desk", {"ok": True, "text": "secret otp 1234"}
        return "pc1", "Desk", {"ok": True}


class RecordingPhoneHub(PhoneHub):
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
        return "Pixel", {"ok": True, "text": "https://example.com"}


class ScriptedEngine(AgentEngine):
    def __init__(self, client_hub: ClientHub, phone_hub: PhoneHub, scripted: list[dict[str, Any]]) -> None:
        super().__init__(client_hub, "http://localhost:1/v1", "test", phone_hub=phone_hub)
        self._scripted = list(scripted)

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._scripted.pop(0) if self._scripted else {"role": "assistant", "content": "Done."}


def tool_msg(name: str, args_json: str, call_id: str = "c1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args_json}}],
    }


class NewToolsPolicyTests(unittest.TestCase):
    def test_all_new_tools_are_registered(self) -> None:
        names = {t["function"]["name"] for t in AGENT_TOOLS}
        expected = {
            "control_pc_audio",
            "control_pc_display",
            "control_pc_power",
            "get_pc_status",
            "arrange_pc_windows",
            "get_pc_layout",
            "read_clipboard",
            "write_clipboard",
            "send_pc_file_to_phone",
        }
        self.assertTrue(expected.issubset(names))

    def test_power_actions_gate_everything_except_cancel(self) -> None:
        for action in ("sleep", "hibernate", "shutdown", "restart"):
            needs, desc = check_confirmation_required("control_pc_power", {"action": action})
            self.assertTrue(needs, action)
            self.assertIn(action, desc.lower())
        self.assertFalse(check_confirmation_required("control_pc_power", {"action": "cancel_shutdown"})[0])

    def test_send_pc_file_always_requires_confirmation(self) -> None:
        needs, desc = check_confirmation_required("send_pc_file_to_phone", {"path": "C:\\report.pdf"})
        self.assertTrue(needs)
        self.assertIn("report.pdf", desc)

    def test_audio_display_layout_status_clipboard_do_not_require_confirmation(self) -> None:
        for name, args in (
            ("control_pc_audio", {"action": "mute_mic"}),
            ("control_pc_display", {"action": "set_brightness", "brightness_pct": 50}),
            ("get_pc_status", {}),
            ("arrange_pc_windows", {"arrangements": [{"target": "Chrome", "slot": "right"}]}),
            ("read_clipboard", {"target": "pc"}),
            ("write_clipboard", {"target": "phone", "text": "hi"}),
        ):
            self.assertFalse(check_confirmation_required(name, args)[0], name)

    def test_summaries(self) -> None:
        self.assertEqual(summarize_tool_call("get_pc_status", {}), "PC Status")
        self.assertEqual(summarize_tool_call("read_clipboard", {"target": "phone"}), "Read PHONE Clipboard")
        self.assertEqual(
            summarize_tool_call("arrange_pc_windows", {"arrangements": [{}, {}]}),
            "Arrange 2 PC windows",
        )


class ClipboardRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_clipboard_routes_between_pc_and_phone_and_taints_turn(self) -> None:
        client = RecordingClientHub()
        phone = RecordingPhoneHub()
        scripted = [
            tool_msg("read_clipboard", '{"target": "pc"}', "c1"),
            tool_msg("write_clipboard", '{"target": "phone", "text": "secret otp 1234"}', "c2"),
            {"role": "assistant", "content": "Copied to your phone."},
        ]
        engine = ScriptedEngine(client, phone, scripted)
        result = await engine.run_turn("Copy my PC clipboard to my phone")

        self.assertEqual(result.status, "completed")
        self.assertEqual(client.calls, [("read_pc_clipboard", {})])
        self.assertEqual(phone.calls, [("clipboard.write", {"text": "secret otp 1234"})])
        self.assertTrue(engine._sessions[result.session_id].screen_tainted)

    async def test_reading_clipboard_gates_subsequent_typing(self) -> None:
        client = RecordingClientHub()
        phone = RecordingPhoneHub()
        scripted = [
            tool_msg("read_clipboard", '{"target": "pc"}', "c1"),
            tool_msg("open_pc_url", '{"url": "https://evil.example"}', "c2"),
        ]
        engine = ScriptedEngine(client, phone, scripted)
        result = await engine.run_turn("Read clipboard and open it")
        self.assertEqual(result.status, "confirmation_required")
        self.assertIn("Screen text was read", result.pending_confirmation["description"])


if __name__ == "__main__":
    unittest.main()
