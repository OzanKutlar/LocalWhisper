"""Unit tests for AgentEngine thinking extraction, tool loop, and confirmation gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_engine import (  # noqa: E402
    AgentEngine,
    check_confirmation_required,
    extract_thinking_and_content,
    summarize_tool_call,
)
from client_hub import ClientHub  # noqa: E402


class FakeClientHub(ClientHub):
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
        return "pc01", "GamingPC", {"ok": True, "executed": tool_name}


class FakeAgentEngine(AgentEngine):
    def __init__(self, hub: FakeClientHub, scripted_messages: list[dict[str, Any]]) -> None:
        super().__init__(hub, llm_base_url="http://localhost:11434/v1", llm_model="qwen3:8b")
        self._scripted = list(scripted_messages)

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._scripted:
            return {"role": "assistant", "content": "Finished."}
        return self._scripted.pop(0)


class ThinkingExtractionTests(unittest.TestCase):
    def test_extracts_think_tags_and_cleans_content(self) -> None:
        msg = {
            "role": "assistant",
            "content": "<think>The user wants to pause music.</think>I paused your music.",
        }
        thought, content = extract_thinking_and_content(msg)
        self.assertEqual(thought, "The user wants to pause music.")
        self.assertEqual(content, "I paused your music.")

    def test_extracts_reasoning_content_field(self) -> None:
        msg = {
            "role": "assistant",
            "reasoning_content": "Checking media keys",
            "content": "Done!",
        }
        thought, content = extract_thinking_and_content(msg)
        self.assertEqual(thought, "Checking media keys")
        self.assertEqual(content, "Done!")


class ConfirmationGatePolicyTests(unittest.TestCase):
    def test_safe_ahk_sequence_does_not_require_confirmation(self) -> None:
        needs, _desc = check_confirmation_required(
            "execute_pc_ahk_sequence",
            {"steps": [{"text": "Hello world{Enter}", "wait_ms": 100}]},
        )
        self.assertFalse(needs)

    def test_destructive_ahk_sequence_requires_confirmation(self) -> None:
        needs, desc = check_confirmation_required(
            "execute_pc_ahk_sequence",
            {"steps": [{"text": "{!F4}", "wait_ms": 100}]},
        )
        self.assertTrue(needs)
        self.assertIn("AHK macro", desc)

    def test_close_window_requires_confirmation(self) -> None:
        needs, desc = check_confirmation_required(
            "manage_pc_app",
            {"action": "close_window", "target": "Chrome"},
        )
        self.assertTrue(needs)
        self.assertIn("Chrome", desc)

    def test_safe_app_launch_does_not_require_confirmation(self) -> None:
        needs, _desc = check_confirmation_required(
            "manage_pc_app",
            {"action": "launch_app", "target": "notepad"},
        )
        self.assertFalse(needs)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_pc_and_android_tools_with_thinking(self) -> None:
        hub = FakeClientHub()
        scripted = [
            {
                "role": "assistant",
                "reasoning_content": "First I will pause PC media and set a 5 minute timer on the phone.",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "control_pc_media",
                            "arguments": '{"action": "play_pause"}',
                        },
                    },
                    {
                        "id": "call_2",
                        "function": {
                            "name": "trigger_android_intent",
                            "arguments": '{"intent_type": "set_timer", "seconds": 300, "label": "Tea"}',
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "<think>Both tools succeeded.</think>Paused your PC and set a 5-minute timer.",
            },
        ]
        engine = FakeAgentEngine(hub, scripted)
        result = await engine.run_turn("Pause my PC and set a timer for 5 minutes")

        self.assertEqual(result.status, "completed")
        self.assertIn("First I will pause PC media", result.thinking)
        self.assertIn("Both tools succeeded.", result.thinking)
        self.assertEqual(result.reply, "Paused your PC and set a 5-minute timer.")
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(hub.calls[0][0], "control_pc_media")
        self.assertEqual(len(result.android_actions), 1)
        self.assertEqual(result.android_actions[0]["intent_type"], "set_timer")

    async def test_confirmation_gate_pauses_and_resumes_on_approval(self) -> None:
        hub = FakeClientHub()
        scripted = [
            {
                "role": "assistant",
                "content": "<think>User asked to close Chrome.</think>",
                "tool_calls": [
                    {
                        "id": "call_close",
                        "function": {
                            "name": "manage_pc_app",
                            "arguments": '{"action": "close_window", "target": "Chrome"}',
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Closed Chrome on your PC.",
            },
        ]
        engine = FakeAgentEngine(hub, scripted)
        first_turn = await engine.run_turn("Close Chrome on my PC")

        self.assertEqual(first_turn.status, "confirmation_required")
        self.assertIsNotNone(first_turn.pending_confirmation)
        self.assertEqual(len(hub.calls), 0)

        conf_id = first_turn.pending_confirmation["confirmation_id"]
        resumed = await engine.confirm_tool(
            session_id=first_turn.session_id,
            confirmation_id=conf_id,
            approved=True,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(hub.calls[0][0], "manage_pc_app")
        self.assertEqual(resumed.reply, "Closed Chrome on your PC.")

    async def test_rehydrates_session_from_client_history(self) -> None:
        hub = FakeClientHub()
        captured_messages: list[dict[str, Any]] = []

        class InspectingEngine(FakeAgentEngine):
            async def _request_chat_completion(
                self, messages: list[dict[str, Any]]
            ) -> dict[str, Any]:
                captured_messages.extend(messages)
                return await super()._request_chat_completion(messages)

        engine = InspectingEngine(
            hub,
            [{"role": "assistant", "content": "Continued session reply."}],
        )
        prior_history = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]
        result = await engine.run_turn(
            prompt="Follow-up question",
            session_id="saved_sess_1",
            history=prior_history,
        )
        self.assertEqual(result.session_id, "saved_sess_1")
        roles_and_texts = [(m["role"], m["content"]) for m in captured_messages if m["role"] != "system"]
        self.assertEqual(
            roles_and_texts,
            [
                ("user", "First question"),
                ("assistant", "First answer"),
                ("user", "Follow-up question"),
            ],
        )

    async def test_inspects_phone_and_pc_screens_and_window_list(self) -> None:
        hub = FakeClientHub()
        scripted = [
            {
                "role": "assistant",
                "reasoning_content": "Let me check the phone screen and list PC windows.",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_phone",
                        "function": {
                            "name": "inspect_phone_screen",
                            "arguments": '{"query": "WhatsApp"}',
                        },
                    },
                    {
                        "id": "call_ps",
                        "function": {
                            "name": "list_pc_windows",
                            "arguments": '{"query": "chrome", "include_minimized": true}',
                        },
                    },
                    {
                        "id": "call_pc_screen",
                        "function": {
                            "name": "inspect_pc_screen",
                            "arguments": '{"include_ocr": true}',
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "You have WhatsApp open on your phone and Chrome open on your PC.",
            },
        ]
        engine = FakeAgentEngine(hub, scripted)
        phone_snapshot = {
            "accessibility_enabled": True,
            "app_name": "WhatsApp",
            "package_name": "com.whatsapp",
            "visible_texts": ["WhatsApp", "Hey, are we still meeting at 5?"],
        }
        result = await engine.run_turn(
            prompt="What is on my phone and PC screen?",
            phone_screen=phone_snapshot,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(hub.calls), 2)
        self.assertEqual(hub.calls[0][0], "list_pc_windows")
        self.assertEqual(hub.calls[1][0], "inspect_pc_screen")
        self.assertEqual(len(result.tool_calls), 3)
        self.assertEqual(result.tool_calls[0]["tool"], "inspect_phone_screen")

    def test_summarize_tool_call_formats_labels(self) -> None:
        label = summarize_tool_call(
            "execute_pc_ahk_sequence",
            {"steps": [{"text": "{^a}"}, {"text": "{^c}"}]},
        )
        self.assertEqual(label, "AHK macro (2 steps)")
        self.assertEqual(
            summarize_tool_call("list_pc_windows", {"query": "code"}),
            "PC Windows ('code')",
        )
        self.assertEqual(
            summarize_tool_call("inspect_pc_screen", {}),
            "View PC Screen",
        )
        self.assertEqual(
            summarize_tool_call("inspect_phone_screen", {}),
            "View Phone Screen",
        )


if __name__ == "__main__":
    unittest.main()
