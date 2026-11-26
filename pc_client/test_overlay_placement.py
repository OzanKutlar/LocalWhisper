"""Unit tests for pc_client/overlay/placement.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overlay.placement import (  # noqa: E402
    WorkArea,
    compute_initial_pill_position,
    compute_popup_position,
    pick_work_area,
    snap_pill_to_edge,
)


class PopupPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mon1 = WorkArea(0, 0, 1920, 1040)
        self.mon2 = WorkArea(1920, 0, 2560, 1400)

    def test_places_centered_below_cursor_when_space_allows(self) -> None:
        x, y = compute_popup_position(960, 300, 440, 320, self.mon1, cursor_offset_y=18, edge_margin=12)
        self.assertEqual(x, 960 - 220)
        self.assertEqual(y, 318)

    def test_flips_above_cursor_near_bottom_edge(self) -> None:
        x, y = compute_popup_position(960, 950, 440, 320, self.mon1, cursor_offset_y=18, edge_margin=12)
        self.assertEqual(x, 960 - 220)
        self.assertEqual(y, 950 - 18 - 320)

    def test_clamps_to_left_and_right_monitor_edges(self) -> None:
        lx, _ly = compute_popup_position(10, 200, 440, 320, self.mon1, edge_margin=12)
        self.assertEqual(lx, 12)
        rx, _ry = compute_popup_position(1915, 200, 440, 320, self.mon1, edge_margin=12)
        self.assertEqual(rx, 1920 - 440 - 12)

    def test_picks_work_area_containing_cursor(self) -> None:
        chosen = pick_work_area(2400, 500, [self.mon1, self.mon2])
        self.assertEqual(chosen, self.mon2)

    def test_pill_initial_and_edge_snap(self) -> None:
        ix, iy = compute_initial_pill_position(180, 40, self.mon1, edge_margin=12, top_offset=24)
        self.assertEqual((ix, iy), (1920 - 180 - 12, 24))

        sx_left, sy_left = snap_pill_to_edge(140, 300, 180, 40, self.mon1, edge_margin=12)
        self.assertEqual((sx_left, sy_left), (12, 300))

        sx_right, _sy = snap_pill_to_edge(1400, 300, 180, 40, self.mon1, edge_margin=12)
        self.assertEqual(sx_right, 1920 - 180 - 12)


if __name__ == "__main__":
    unittest.main()
