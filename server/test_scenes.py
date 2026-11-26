"""Unit tests for server/scenes.py and scene wiring in AgentEngine."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_engine import AgentEngine  # noqa: E402
from client_hub import ClientHub  # noqa: E402
from phone_hub import PhoneHub  # noqa: E402
from scenes import (  # noqa: E402
    MAX_WAIT_MS,
    Scene,
    SceneRunner,
    SceneStep,
    SceneStore,
    layout_to_arrange_steps,
    parse_scene_steps,
)

LAYOUT: dict[str, Any] = {
    "ok": True,
    "windows": [
        {
            "title": "main.py - Code",
            "process_name": "Code.exe",
            "state": "normal",
            "monitor": 1,
            "bounds": {"x": 0, "y": 0, "width": 960, "height": 1040},
        },
        {
            "title": "Docs - Chrome",
            "process_name": "chrome.exe",
            "state": "maximized",
            "monitor": 2,
            "bounds": {"x": 1920, "y": 0, "width": 2560, "height": 1400},
        },
        {
            "title": "notes.md - Code",
            "process_name": "Code.exe",
            "state": "normal",
            "monitor": 1,
            "bounds": {"x": 960, "y": 0, "width": 960, "height": 1040},
        },
        {
            "title": "Spotify",
            "process_name": "Spotify.exe",
            "state": "minimized",
            "monitor": 1,
            "bounds": {"x": 0, "y": 0, "width": 0, "height": 0},
        },
    ],
}


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
        if tool_name == "get_pc_layout":
            return "pc01", "Desk", LAYOUT
        return "pc01", "Desk", {"ok": True}

    def tools(self) -> list[str]:
        return [name for name, _args in self.calls]


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
        return "Pixel", {"ok": True}


class ScriptedEngine(AgentEngine):
    def __init__(self, store: SceneStore, scripted: list[dict[str, Any]]) -> None:
        self.client = RecordingClientHub()
        self.phone = RecordingPhoneHub()
        super().__init__(
            self.client,
            "http://localhost:1/v1",
            "test",
            phone_hub=self.phone,
            scene_store=store,
        )
        self._scripted = list(scripted)

    async def _request_chat_completion(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._scripted.pop(0) if self._scripted else {"role": "assistant", "content": "Done."}


def tool_msg(name: str, args_json: str, call_id: str = "c1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args_json}}],
    }


def reply(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


class SceneParsingTests(unittest.TestCase):
    def test_parses_tools_waits_and_trailing_wait_ms(self) -> None:
        steps = parse_scene_steps(
            [
                {"tool": "control_pc_audio", "arguments": {"action": "set_volume", "volume_pct": 40}, "wait_ms": 300},
                {"wait_ms": 99999},
            ]
        )
        self.assertEqual([step.tool for step in steps], ["control_pc_audio", "wait", "wait"])
        self.assertEqual(steps[1].arguments, {"ms": 300})
        self.assertEqual(steps[2].arguments, {"ms": MAX_WAIT_MS})

    def test_rejects_disallowed_tools_and_bad_arguments(self) -> None:
        with self.assertRaises(ValueError):
            parse_scene_steps([{"tool": "click_screen_element"}])
        with self.assertRaises(ValueError):
            parse_scene_steps([{"tool": "run_scene", "arguments": {"name": "x"}}])
        with self.assertRaises(ValueError):
            parse_scene_steps([{"tool": "control_pc_audio", "arguments": "mute"}])
        with self.assertRaises(ValueError):
            parse_scene_steps("nope")

    def test_layout_becomes_one_arrange_step_per_app(self) -> None:
        steps = layout_to_arrange_steps(LAYOUT)
        self.assertEqual(len(steps), 1)
        items = steps[0].arguments["arrangements"]
        self.assertEqual([item["target"] for item in items], ["Code", "chrome"])
        self.assertEqual(items[0]["slot"], "custom")
        self.assertEqual(items[0]["rect"], {"x": 0, "y": 0, "width": 960, "height": 1040})
        self.assertEqual((items[1]["slot"], items[1]["monitor"]), ("maximize", "2"))
        self.assertEqual(layout_to_arrange_steps(None), [])


class SceneStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "scenes.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_seeds_starter_scenes_and_persists_changes(self) -> None:
        store = SceneStore(self.path)
        self.assertEqual(store.names(), ["End of Day", "Meeting Mode", "Start Work"])
        store.save("Focus", "Quiet time", [SceneStep("control_pc_media", {"action": "mute"})])
        self.assertTrue(store.delete("start work"))

        reloaded = SceneStore(self.path)
        self.assertIsNotNone(reloaded.get("focus"))
        self.assertIsNone(reloaded.get("Start Work"))

    def test_rejects_empty_and_wait_only_scenes(self) -> None:
        store = SceneStore(self.path)
        with self.assertRaises(ValueError):
            store.save("Empty", "", [])
        with self.assertRaises(ValueError):
            store.save("Waits", "", [SceneStep("wait", {"ms": 100})])
        with self.assertRaises(ValueError):
            store.save("   ", "", [SceneStep("control_pc_media", {"action": "mute"})])


class SceneRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _scene(self, continue_on_error: bool) -> Scene:
        return Scene(
            "Test",
            "",
            (
                SceneStep("open_pc_url", {"url": "example.com"}),
                SceneStep("wait", {"ms": 250}),
                SceneStep("control_pc_media", {"action": "mute"}),
            ),
            continue_on_error,
        )

    async def _run(self, scene: Scene) -> tuple[dict[str, Any], list[str], list[float]]:
        calls: list[str] = []
        sleeps: list[float] = []

        async def executor(tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append(tool)
            return {"ok": False, "error": "boom"} if tool == "open_pc_url" else {"ok": True}

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        result = await SceneRunner(executor, sleep=fake_sleep).run(scene)
        return result, calls, sleeps

    async def test_runs_in_order_and_continues_after_errors(self) -> None:
        result, calls, sleeps = await self._run(self._scene(continue_on_error=True))
        self.assertEqual(calls, ["open_pc_url", "control_pc_media"])
        self.assertEqual(sleeps, [0.25])
        self.assertFalse(result["ok"])
        self.assertEqual((result["steps_run"], result["steps_ok"]), (3, 2))
        self.assertEqual(result["results"][0]["error"], "boom")

    async def test_stops_on_error_when_configured(self) -> None:
        result, calls, _sleeps = await self._run(self._scene(continue_on_error=False))
        self.assertEqual(calls, ["open_pc_url"])
        self.assertTrue(result["stopped_early"])


class SceneEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SceneStore(Path(self._tmp.name) / "scenes.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_gated_scene_asks_once_then_runs_every_step(self) -> None:
        engine = ScriptedEngine(self.store, [tool_msg("run_scene", '{"name": "end of day"}'), reply("Good night.")])
        first = await engine.run_turn("Run end of day")
        self.assertEqual(first.status, "confirmation_required")
        self.assertIn("End of Day", first.pending_confirmation["description"])
        self.assertEqual(engine.client.calls, [])
        self.assertEqual(engine.phone.calls, [])

        resumed = await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=True,
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(engine.client.tools(), ["manage_pc_app"])
        self.assertEqual([command for command, _args in engine.phone.calls], ["phone.control", "phone.control"])
        self.assertIn("3/3 steps ok", resumed.tool_calls[-1]["summary"])

    async def test_ungated_scene_runs_without_approval(self) -> None:
        engine = ScriptedEngine(self.store, [tool_msg("run_scene", '{"name": "Start Work"}'), reply("Ready.")])
        result = await engine.run_turn("Start work")
        self.assertEqual(result.status, "completed")
        self.assertEqual(engine.client.tools(), ["control_pc_audio"])
        self.assertEqual(engine.phone.calls[0][0], "phone.control")

    async def test_unknown_scene_is_reported_without_approval(self) -> None:
        engine = ScriptedEngine(self.store, [tool_msg("run_scene", '{"name": "Gaming"}'), reply("No such scene.")])
        result = await engine.run_turn("Run gaming")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls[0]["status"], "error")

    async def test_save_scene_captures_layout_after_approval(self) -> None:
        scripted = [
            tool_msg("save_scene", '{"name": "Coding", "capture_current_pc_layout": true}'),
            reply("Saved."),
        ]
        engine = ScriptedEngine(self.store, scripted)
        first = await engine.run_turn("Save my layout as Coding")
        self.assertEqual(first.status, "confirmation_required")
        self.assertIsNone(self.store.get("coding"))

        await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=True,
        )
        scene = self.store.get("coding")
        self.assertIsNotNone(scene)
        self.assertEqual([step.tool for step in scene.steps], ["arrange_pc_windows"])
        self.assertIn("get_pc_layout", engine.client.tools())

    async def test_layout_save_into_existing_scene_keeps_its_other_steps(self) -> None:
        scripted = [
            tool_msg("save_scene", '{"name": "Start Work", "capture_current_pc_layout": true}'),
            reply("Updated."),
        ]
        engine = ScriptedEngine(self.store, scripted)
        first = await engine.run_turn("Save my current window layout as Start Work")
        await engine.confirm_tool(
            session_id=first.session_id,
            confirmation_id=first.pending_confirmation["confirmation_id"],
            approved=True,
        )
        scene = self.store.get("start work")
        self.assertEqual(
            [step.tool for step in scene.steps],
            ["control_pc_audio", "control_phone", "arrange_pc_windows"],
        )
        self.assertTrue(scene.description)

    def test_describe_scenes_flags_which_ones_need_approval(self) -> None:
        engine = ScriptedEngine(self.store, [])
        flags = {item["name"]: item["needs_approval"] for item in engine.describe_scenes()}
        self.assertEqual(flags, {"End of Day": True, "Meeting Mode": False, "Start Work": False})


if __name__ == "__main__":
    unittest.main()
