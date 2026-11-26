"""Unit tests for pc_client/overlay/timeline.py and session_store.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overlay.session_store import AgentChatTurn, PcSessionStore  # noqa: E402
from overlay.timeline import (  # noqa: E402
    ThinkingEntry,
    TimelineEntry,
    ToolEntry,
    append_thinking,
    reconcile_tools,
    thinking_text,
    tool_count,
)


class TimelineTests(unittest.TestCase):
    def test_interleaves_thinking_and_reconciles_tools_in_place(self) -> None:
        entries: list[TimelineEntry] = []
        append_thinking(entries, "Step 1")
        append_thinking(entries, ": check audio.")
        reconcile_tools(entries, [{"tool": "control_pc_audio", "status": "running", "summary": "Running"}])
        reconcile_tools(entries, [{"tool": "control_pc_audio", "status": "ok", "summary": "Muted mic"}])
        append_thinking(entries, "\n\nDone thinking.")

        self.assertEqual(
            entries,
            [
                ThinkingEntry("Step 1: check audio."),
                ToolEntry("control_pc_audio", "ok", "Muted mic"),
                ThinkingEntry("Done thinking."),
            ],
        )
        self.assertEqual(tool_count(entries), 1)
        self.assertEqual(thinking_text(entries), "Step 1: check audio.\n\nDone thinking.")


class SessionStoreTests(unittest.TestCase):
    def test_persists_turns_and_honors_resume_timeout(self) -> None:
        now = [1000.0]
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sessions.json"
            store = PcSessionStore(path=path, clock=lambda: now[0])
            sid = store.start_new_session()
            turn = AgentChatTurn(
                user_prompt="Mute my mic",
                reply="Muted.",
                timeline=[ThinkingEntry("Muting"), ToolEntry("control_pc_audio", "ok", "Muted")],
            )
            store.record_turn(sid, turn)
            store.record_overlay_closed()

            now[0] += 5.0
            reloaded = PcSessionStore(path=path, clock=lambda: now[0])
            self.assertEqual(reloaded.resolve_initial_session_id(15), sid)

            now[0] += 30.0
            self.assertIsNone(reloaded.resolve_initial_session_id(15))


if __name__ == "__main__":
    unittest.main()
