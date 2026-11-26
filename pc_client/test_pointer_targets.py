"""Unit tests for aimed scrolling/hovering. Run: python -m unittest discover -s pc_client -p "test_*.py" -v"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pc_tools  # noqa: E402
from screen_elements import (  # noqa: E402
    ScreenshotFrame,
    box_2d_to_region_point,
    frame_staleness_reason,
)

AHK = Path("AutoHotkeyU64.exe")
REGION = {"x": 100, "y": 50, "width": 1000, "height": 800}


def fake_after(target: dict, action: str) -> dict:
    return {"ok": True, action: True, "point": {"x": target["x"], "y": target["y"]}}


class BoxMappingTests(unittest.TestCase):
    def test_maps_box_center_into_region(self) -> None:
        mapped = box_2d_to_region_point([250, 100, 750, 300], REGION)
        self.assertTrue(mapped["ok"])
        self.assertEqual((mapped["x"], mapped["y"]), (300, 450))
        self.assertEqual(mapped["box"], {"x": 200, "y": 250, "w": 200, "h": 400})

    def test_rejects_bad_boxes_and_regions(self) -> None:
        self.assertEqual(box_2d_to_region_point([1, 2, 3], REGION)["reason"], "bad_box")
        self.assertEqual(box_2d_to_region_point([10, 10, 10, 50], REGION)["reason"], "empty_box")
        self.assertEqual(box_2d_to_region_point([0, 0, 10, 10], None)["reason"], "no_screenshot")

    def test_frame_staleness(self) -> None:
        now = time.monotonic()
        rect = dict(REGION)
        frame = ScreenshotFrame(region=dict(REGION), hwnd=7, window_rect=rect, captured_at=now)
        self.assertIsNone(frame_staleness_reason(frame, dict(rect), now))
        self.assertIn("moved", frame_staleness_reason(frame, dict(rect, x=400), now))
        self.assertIn("no longer open", frame_staleness_reason(frame, None, now))
        self.assertIn("old", frame_staleness_reason(frame, dict(rect), now + 9999))


class PointerTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        pc_tools.FRAMES.clear()

    def tearDown(self) -> None:
        pc_tools.FRAMES.clear()

    def remember(self, hwnd: int = 0, window_rect: dict | None = None) -> None:
        pc_tools.FRAMES.remember(
            ScreenshotFrame(region=dict(REGION), hwnd=hwnd, window_rect=window_rect, captured_at=time.monotonic())
        )

    def run_with_mocks(self, func, arguments: dict) -> tuple[dict, mock.MagicMock, mock.MagicMock]:
        with (
            mock.patch("pc_tools._run_mouse_ahk") as ahk,
            mock.patch("pc_tools._focus_target_window") as focus,
            mock.patch("pc_tools._after_action_result", side_effect=fake_after),
            mock.patch("pc_tools._root_window_at", return_value=0),
            mock.patch("pc_tools._get_window_title", return_value=""),
        ):
            result = func(arguments, AHK)
        return result, ahk, focus

    def test_scroll_box_2d_aims_inside_screenshot_region(self) -> None:
        self.remember()
        result, ahk, _focus = self.run_with_mocks(
            pc_tools.run_scroll_pc, {"direction": "down", "box_2d": [250, 100, 750, 300], "amount": 2}
        )
        self.assertTrue(result["ok"])
        ahk.assert_called_once_with(AHK, ["scroll", "300", "450", "down", "2"])

    def test_scroll_point_uses_given_coordinates(self) -> None:
        result, ahk, _focus = self.run_with_mocks(pc_tools.run_scroll_pc, {"direction": "up", "x": 40, "y": 600})
        self.assertTrue(result["ok"])
        ahk.assert_called_once_with(AHK, ["scroll", "40", "600", "up", "3"])

    def test_scroll_without_target_uses_window_center(self) -> None:
        center = {"ok": True, "mode": "point", "hwnd": 0, "element_id": "", "display_text": "",
                  "window_title": "Code", "x": 500, "y": 400}
        with mock.patch("pc_tools._foreground_center_target", return_value=center):
            _result, ahk, _focus = self.run_with_mocks(pc_tools.run_scroll_pc, {"direction": "down"})
        ahk.assert_called_once_with(AHK, ["scroll", "500", "400", "down", "3"])

    def test_box_2d_without_screenshot_is_rejected(self) -> None:
        result, ahk, _focus = self.run_with_mocks(
            pc_tools.run_scroll_pc, {"direction": "down", "box_2d": [0, 0, 500, 500]}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_screenshot")
        ahk.assert_not_called()

    def test_box_2d_rejected_after_window_moved(self) -> None:
        rect = dict(REGION)
        self.remember(hwnd=77, window_rect=rect)
        with mock.patch("pc_tools._get_window_rect", return_value=dict(rect, x=400)):
            result, ahk, _focus = self.run_with_mocks(
                pc_tools.run_scroll_pc, {"direction": "down", "box_2d": [0, 0, 500, 500]}
            )
        self.assertEqual(result["reason"], "stale_screenshot")
        ahk.assert_not_called()

    def test_move_requires_a_target(self) -> None:
        result, ahk, _focus = self.run_with_mocks(pc_tools.run_move_pc_mouse, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_target")
        ahk.assert_not_called()

    def test_move_hovers_at_point_without_focusing(self) -> None:
        result, ahk, focus = self.run_with_mocks(pc_tools.run_move_pc_mouse, {"x": 10, "y": 20})
        self.assertTrue(result["ok"])
        ahk.assert_called_once_with(AHK, ["move", "10", "20"])
        focus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
