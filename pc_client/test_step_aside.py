"""Unit tests for pc_client/overlay/step_aside.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overlay.step_aside import StepAsideCoordinator, StepAsideState, requires_step_aside  # noqa: E402


class StepAsideTests(unittest.TestCase):
    def test_hides_visible_overlay_and_restores_after_work(self) -> None:
        state = StepAsideState()
        state.on_overlay_shown(previous_hwnd=42)
        self.assertTrue(state.begin_pc_work())
        self.assertFalse(state.overlay_visible)
        self.assertTrue(state.hidden_by_agent)
        self.assertTrue(state.end_pc_work())
        self.assertTrue(state.should_restore_now())
        self.assertTrue(state.overlay_visible)

    def test_does_not_restore_when_user_hid_overlay(self) -> None:
        state = StepAsideState()
        state.on_user_hid_overlay()
        self.assertFalse(state.begin_pc_work())
        self.assertFalse(state.end_pc_work())
        self.assertFalse(state.should_restore_now())

    def test_coordinator_invokes_hide_and_restore_callbacks(self) -> None:
        events: list[str] = []
        coord = StepAsideCoordinator(
            hide_on_ui=lambda: events.append("hide"),
            restore_on_ui=lambda: events.append("restore"),
            grace_s=0.0,
            settle_s=0.0,
        )
        coord.state.on_overlay_shown(previous_hwnd=0)
        out = coord.run_with_step_aside(lambda: (events.append("work"), "ok")[1])
        self.assertEqual(out, "ok")
        self.assertEqual(events, ["hide", "work", "restore"])
        self.assertTrue(requires_step_aside("inspect_pc_screen"))
        self.assertFalse(requires_step_aside("get_pc_status"))


if __name__ == "__main__":
    unittest.main()
