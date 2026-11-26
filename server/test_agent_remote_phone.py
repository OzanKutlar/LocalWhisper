"""Tests for phone tools driven from the PC popup over the phone link, and prompt image attachments."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_engine import AgentEngine, build_android_intent_payload, parse_prompt_attachments  # noqa: E402
from client_hub import ClientHub  # noqa: E402
from phone_hub import NoPhoneConnectedError, PhoneHub  # noqa: E402

PC = "pc_overlay"
PREVIEW_B64 = "UFJFVklFVw=="
PHONE_SCREEN: dict[str, Any] = {
    "accessibility_enabled": True,
    "phone_snapshot_id": "ps7",
    "app_name": "Gmail",
    "package_name": "com.google.android.gm",
    "screen_width": 1080,
    "screen_height": 2400,
    "ui_elements": [
        {
            "element_id": "p1",
            "role": "Button",
            "content_desc": "Compose",
            "clickable": True,
            "bounds": {"left": 760, "top": 2000, "right": 1020, "bottom": 2140},
        }
    ],
}
AFTER_SCREEN = dict(PHONE_SCREEN, phone_snapshot_id="ps8")


class RecordingClientHub(ClientHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        client_id: str | None = None,
        timeout_s: float = 30.0,
    ) -> tuple[str, str, dict[str, object]]:
        self.calls.append(tool_name)
        return "pc01", "GamingPC", {"ok": True}


class FakePhoneHub(PhoneHub):
    """Answers phone commands from a table instead of a real WebSocket."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None, offline: bool = False) -> None:
        super().__init__()
        self.responses = responses or {}
        self.offline = offline
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        command: str,
        arguments: dict[str, Any],
        timeout_s: float = 15.0,
    ) -> tuple[str, dict[str, Any]]:
        self.calls.append((command, arguments))
        if self.offline:
            raise NoPhoneConnectedError("No phone is connected to the phone link.")
        return "Pixel", self.responses.get(command, {"ok": True})

    def commands(self) -> list[str]:
        return [command for command, _arguments in self.calls]


class ScriptedEngine(AgentEngine):
    def __init__(self, phone_hub: PhoneHub | None, scripted: list[dict[str, Any]]) -> None:
        self.client = RecordingClientHub()
        super().__init__(
            self.client,
            llm_base_url="http://localhost:1/v1",
            llm_model="test",
            phone_hub=phone_hub,
        )
        self._scripted = list(scripted)
        self.seen: list[list[dict[str, Any]]] = []

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.seen.append([dict(message) for message in messages])
        if not self._scripted:
            return {"role": "assistant", "content": "Finished."}
        return self._scripted.pop(0)


def tool_message(name: str, arguments_json: str, call_id: str = "call_x") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments_json}}],
    }


def reply(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def phone_hub_with_screen(**overrides: dict[str, Any]) -> FakePhoneHub:
    responses: dict[str, dict[str, Any]] = {
        "phone.snapshot": {"phone_screen": PHONE_SCREEN},
        "phone.target_preview": {"image_jpeg_b64": PREVIEW_B64},
        "phone.ui_action": {
            "action_result": {"ok": True, "screen_changed": True},
            "phone_screen": AFTER_SCREEN,
        },
    }
    responses.update(overrides)
    return FakePhoneHub(responses)


CLICK_P1 = tool_message("click_phone_element", '{"element_id": "p1"}')
SCROLL_DOWN = tool_message("scroll_phone", '{"direction": "down"}')


class RemotePhoneControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_tap_from_pc_is_previewed_approved_then_run_over_the_link(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [CLICK_P1, reply("Tapped Compose.")])
        first = await engine.run_turn("Tap compose on my phone", origin=PC)

        self.assertEqual(first.status, "confirmation_required")
        pending = first.pending_confirmation
        self.assertEqual(pending["preview_image_jpeg_b64"], PREVIEW_B64)
        self.assertEqual(pending["phone_action"]["element_id"], "p1")
        self.assertEqual(hub.commands(), ["phone.snapshot", "phone.target_preview"])

        resumed = await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=pending["confirmation_id"],
            approved=True,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.reply, "Tapped Compose.")
        self.assertEqual([call["status"] for call in resumed.tool_calls], ["ok"])
        command, arguments = hub.calls[-1]
        self.assertEqual(command, "phone.ui_action")
        self.assertEqual(arguments["action"]["element_id"], "p1")
        self.assertEqual(engine._sessions[first.session_id].phone_screen["phone_snapshot_id"], "ps8")

    async def test_rejected_tap_never_reaches_the_phone(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [CLICK_P1, reply("OK, I left it alone.")])
        first = await engine.run_turn("Tap compose", origin=PC)
        resumed = await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=False,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertNotIn("phone.ui_action", hub.commands())

    async def test_scroll_from_pc_runs_immediately(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [SCROLL_DOWN, reply("Scrolled.")])
        result = await engine.run_turn("Scroll down on my phone", origin=PC)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.pending_confirmation)
        self.assertEqual(hub.commands(), ["phone.snapshot", "phone.ui_action"])
        self.assertEqual([call["status"] for call in result.tool_calls], ["ok"])

    async def test_scroll_from_pc_streams_running_then_done(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [SCROLL_DOWN, reply("Scrolled.")])
        events = [event async for event in engine.run_turn_stream("Scroll down on my phone", origin=PC)]
        running = [
            call
            for event in events
            if event["type"] == "tool_calls"
            for call in event["tool_calls"]
            if call["status"] == "running"
        ]
        self.assertTrue(running)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["result"]["status"], "completed")
        self.assertIn("phone.ui_action", hub.commands())

    async def test_screenshot_from_pc_attaches_the_image(self) -> None:
        capture = {
            "action_result": {
                "ok": True,
                "action": "capture_screen",
                "image_jpeg_b64": "UEhPTkU=",
                "mime_type": "image/jpeg",
                "width": 540,
                "height": 1200,
            },
            "phone_screen": AFTER_SCREEN,
        }
        hub = phone_hub_with_screen(**{"phone.ui_action": capture})
        engine = ScriptedEngine(hub, [tool_message("capture_phone_screenshot", "{}"), reply("Inbox.")])
        result = await engine.run_turn("What's on my phone?", origin=PC)
        self.assertEqual(result.status, "completed")
        image_urls = [
            part["image_url"]["url"]
            for message in engine.seen[-1]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        self.assertEqual(image_urls[-1], "data:image/jpeg;base64,UEhPTkU=")

    async def test_intent_from_pc_goes_over_the_link(self) -> None:
        hub = FakePhoneHub({"android.intent": {"ok": True, "outcome": "Timer set (60s)"}})
        timer = tool_message("trigger_android_intent", '{"intent_type": "set_timer", "seconds": 60}')
        engine = ScriptedEngine(hub, [timer, reply("Timer set.")])
        result = await engine.run_turn("Set a one minute timer on my phone", origin=PC)
        self.assertEqual(result.android_actions, [])
        self.assertEqual(result.tool_calls[0]["status"], "ok")
        command, arguments = hub.calls[0]
        self.assertEqual(command, "android.intent")
        self.assertEqual((arguments["action"]["intent_type"], arguments["action"]["seconds"]), ("set_timer", 60))

    async def test_offline_phone_is_reported_without_an_approval(self) -> None:
        hub = FakePhoneHub(offline=True)
        engine = ScriptedEngine(hub, [CLICK_P1, reply("Your phone is offline.")])
        result = await engine.run_turn("Tap compose", origin=PC)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls[0]["status"], "error")
        self.assertIn("phone_offline", result.tool_calls[0]["summary"])
        self.assertEqual(hub.commands(), ["phone.snapshot"])

    async def test_stop_on_the_phone_halts_an_approved_tap(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [CLICK_P1, reply("Stopped.")])
        first = await engine.run_turn("Tap compose", origin=PC)
        await hub.handle_message("phone1", json.dumps({"type": "event", "event": "remote_stop", "data": {}}))
        resumed = await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=True,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertNotIn("phone.ui_action", hub.commands())
        self.assertEqual([call["status"] for call in resumed.tool_calls], ["error"])

    async def test_phone_origin_still_uses_the_phone_overlay_flow(self) -> None:
        hub = phone_hub_with_screen()
        engine = ScriptedEngine(hub, [CLICK_P1])
        first = await engine.run_turn("Tap compose", phone_screen=PHONE_SCREEN)
        self.assertEqual(first.status, "confirmation_required")
        self.assertEqual(first.pending_confirmation["preview_image_jpeg_b64"], "")
        self.assertEqual(hub.calls, [])


class PromptAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def test_only_valid_images_are_kept_and_at_most_two(self) -> None:
        parsed = parse_prompt_attachments(
            [
                {"mime_type": "image/jpeg", "data_b64": "LzlqL0FBPT0=", "label": "Whiteboard", "width": 800},
                {"mime_type": "text/plain", "data_b64": "aGk="},
                {"mime_type": "image/png", "data_b64": "UE5H"},
            ]
        )
        self.assertEqual([(a.label, a.width) for a in parsed], [("Whiteboard", 800)])
        self.assertEqual(parse_prompt_attachments([{"mime_type": "image/png", "data_b64": "not base64!!"}]), [])
        self.assertEqual(parse_prompt_attachments("nope"), [])

    async def test_attachments_reach_the_model_and_taint_the_turn(self) -> None:
        engine = ScriptedEngine(None, [reply("It says: ship on Friday.")])
        result = await engine.run_turn(
            "What does this whiteboard say?",
            attachments=[{"mime_type": "image/jpeg", "data_b64": "LzlqL0FBPT0=", "label": "Camera"}],
        )
        self.assertEqual(result.status, "completed")
        last = engine.seen[0][-1]
        self.assertEqual(last["role"], "user")
        urls = [part["image_url"]["url"] for part in last["content"] if part.get("type") == "image_url"]
        self.assertEqual(urls, ["data:image/jpeg;base64,LzlqL0FBPT0="])
        self.assertTrue(engine._sessions[result.session_id].screen_tainted)

    def test_intent_payload_tolerates_non_numbers(self) -> None:
        payload = build_android_intent_payload({"hour": "seven", "minute": "30", "seconds": True})
        self.assertEqual((payload["hour"], payload["minute"], payload["seconds"]), (-1, 30, 0))


if __name__ == "__main__":
    unittest.main()
