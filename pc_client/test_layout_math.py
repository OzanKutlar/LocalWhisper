"""Unit tests for pc_client/layout_math.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from layout_math import (  # noqa: E402
    MonitorInfo,
    compensate_invisible_borders,
    compute_slot_rect,
    find_window_monitor,
    normalize_slot,
    resolve_monitor,
    sort_monitors_left_to_right,
)


class MonitorSortingTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = [
            MonitorInfo(0, "DISPLAY2", False, 1920, 0, 2560, 1400),
            MonitorInfo(0, "DISPLAY1", True, 0, 0, 1920, 1040),
            MonitorInfo(0, "DISPLAY3", False, -1280, 0, 1280, 1024),
        ]
        self.monitors = sort_monitors_left_to_right(raw)

    def test_numbers_monitors_left_to_right(self) -> None:
        self.assertEqual([m.name for m in self.monitors], ["DISPLAY3", "DISPLAY1", "DISPLAY2"])
        self.assertEqual([m.index for m in self.monitors], [1, 2, 3])

    def test_resolve_monitor_by_index_primary_and_name(self) -> None:
        self.assertEqual(resolve_monitor(self.monitors, "primary").name, "DISPLAY1")
        self.assertEqual(resolve_monitor(self.monitors, None).name, "DISPLAY1")
        self.assertEqual(resolve_monitor(self.monitors, 1).name, "DISPLAY3")
        self.assertEqual(resolve_monitor(self.monitors, "3").name, "DISPLAY2")
        self.assertEqual(resolve_monitor(self.monitors, "monitor 2").name, "DISPLAY1")
        self.assertEqual(resolve_monitor(self.monitors, "display2").name, "DISPLAY2")

    def test_resolve_monitor_rejects_invalid_specs(self) -> None:
        with self.assertRaises(ValueError):
            resolve_monitor(self.monitors, 9)
        with self.assertRaises(ValueError):
            resolve_monitor(self.monitors, True)
        with self.assertRaises(ValueError):
            resolve_monitor([], 1)


class SlotRectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mon = MonitorInfo(1, "DISPLAY1", True, 100, 50, 1920, 1040)

    def test_halves_and_quarters(self) -> None:
        self.assertEqual(compute_slot_rect(self.mon, "left"), {"x": 100, "y": 50, "width": 960, "height": 1040})
        self.assertEqual(compute_slot_rect(self.mon, "right"), {"x": 1060, "y": 50, "width": 960, "height": 1040})
        self.assertEqual(compute_slot_rect(self.mon, "bottom_right"), {"x": 1060, "y": 570, "width": 960, "height": 520})

    def test_thirds_cover_full_width_without_gaps(self) -> None:
        left = compute_slot_rect(self.mon, "left_third")
        mid = compute_slot_rect(self.mon, "center_third")
        right = compute_slot_rect(self.mon, "right_third")
        self.assertEqual(left["x"] + left["width"], mid["x"])
        self.assertEqual(mid["x"] + mid["width"], right["x"])
        self.assertEqual(right["x"] + right["width"], self.mon.work_x + self.mon.work_w)

    def test_custom_fractional_and_clamped_pixel_rects(self) -> None:
        frac = compute_slot_rect(self.mon, "custom", {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5})
        self.assertEqual(frac, {"x": 580, "y": 310, "width": 960, "height": 520})
        clamped = compute_slot_rect(self.mon, "custom", {"x": 5000, "y": 5000, "width": 400, "height": 300})
        self.assertEqual(clamped, {"x": 1620, "y": 790, "width": 400, "height": 300})

    def test_aliases_and_invalid_slots(self) -> None:
        self.assertEqual(normalize_slot("left-half"), "left")
        self.assertEqual(normalize_slot("centered"), "center")
        with self.assertRaises(ValueError):
            normalize_slot("diagonal")

    def test_invisible_border_compensation(self) -> None:
        target = {"x": 0, "y": 0, "width": 960, "height": 1040}
        win_rect = {"x": 100, "y": 100, "width": 814, "height": 607}
        ext_frame = {"x": 107, "y": 100, "width": 800, "height": 600}
        adjusted = compensate_invisible_borders(target, win_rect, ext_frame)
        self.assertEqual(adjusted, {"x": -7, "y": 0, "width": 974, "height": 1047})

    def test_find_window_monitor_picks_largest_overlap(self) -> None:
        mons = [
            MonitorInfo(1, "D1", True, 0, 0, 1920, 1080),
            MonitorInfo(2, "D2", False, 1920, 0, 1920, 1080),
        ]
        self.assertEqual(find_window_monitor({"x": 1800, "y": 100, "width": 600, "height": 400}, mons), 2)
        self.assertEqual(find_window_monitor({"x": 100, "y": 100, "width": 600, "height": 400}, mons), 1)


if __name__ == "__main__":
    unittest.main()
