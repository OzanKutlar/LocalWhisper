"""Unit tests for pc_client/overlay/style.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overlay import style  # noqa: E402


class StyleTests(unittest.TestCase):
    def tearDown(self) -> None:
        style.set_ui_scale(style.DEFAULT_UI_SCALE)

    def test_px_scales_and_never_returns_zero(self) -> None:
        style.set_ui_scale(1.5)
        self.assertEqual(style.px(10), 15)
        self.assertEqual(style.px(13.5), 20)
        self.assertEqual(style.px(0), 1)

    def test_scale_is_clamped_and_garbage_falls_back(self) -> None:
        self.assertEqual(style.set_ui_scale(9), style.MAX_UI_SCALE)
        self.assertEqual(style.set_ui_scale(0.1), style.MIN_UI_SCALE)
        self.assertEqual(style.set_ui_scale("big"), style.DEFAULT_UI_SCALE)
        self.assertEqual(style.set_ui_scale(float("nan")), style.DEFAULT_UI_SCALE)
        self.assertEqual(style.set_ui_scale(True), style.DEFAULT_UI_SCALE)


if __name__ == "__main__":
    unittest.main()
