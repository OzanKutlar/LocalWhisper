"""Unit tests for server/phone_elements.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phone_elements import (  # noqa: E402
    box_2d_to_screen_box,
    format_phone_screen_payload,
    parse_phone_elements,
    resolve_phone_target,
    screen_size_from,
)

SAMPLE_PHONE_SCREEN = {
    "accessibility_enabled": True,
    "phone_snapshot_id": "ps4",
    "app_name": "Gmail",
    "package_name": "com.google.android.gm",
    "ui_elements": [
        {
            "element_id": "p1",
            "role": "EditText",
            "hint": "Search in mail",
            "clickable": True,
            "editable": True,
            "focused": False,
            "bounds": {"left": 40, "top": 120, "right": 1040, "bottom": 240},
        },
        {
            "element_id": "p2",
            "role": "Button",
            "content_desc": "Compose",
            "clickable": True,
            "editable": False,
            "focused": False,
            "bounds": {"left": 760, "top": 2000, "right": 1020, "bottom": 2140},
        },
        {
            "element_id": "p3",
            "role": "TextView",
            "text": "Reply",
            "clickable": True,
            "editable": False,
            "focused": False,
            "bounds": {"left": 100, "top": 900, "right": 300, "bottom": 980},
        },
        {
            "element_id": "p4",
            "role": "TextView",
            "text": "Reply",
            "clickable": True,
            "editable": False,
            "focused": False,
            "bounds": {"left": 100, "top": 500, "right": 300, "bottom": 580},
        },
    ],
}


class PhoneElementsTests(unittest.TestCase):
    def test_parses_and_formats_compact_phone_elements(self) -> None:
        elements = parse_phone_elements(SAMPLE_PHONE_SCREEN)
        self.assertEqual(len(elements), 4)
        self.assertEqual(elements[0].element_id, "p1")
        self.assertEqual(elements[0].display_label(), "Search in mail")
        self.assertEqual(elements[0].center(), (540, 180))

        payload = format_phone_screen_payload(SAMPLE_PHONE_SCREEN)
        self.assertEqual(payload["phone_snapshot_id"], "ps4")
        self.assertIn('p2 "Compose" [Button, clickable] (760,2000 260x140)', payload["elements"])

    def test_resolves_by_element_id_and_offset(self) -> None:
        resolved = resolve_phone_target(SAMPLE_PHONE_SCREEN, {"element_id": "p2", "offset_x": -10})
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["element_id"], "p2")
        self.assertEqual(resolved["display_text"], "Compose")
        self.assertEqual((resolved["x"], resolved["y"]), (880, 2070))

    def test_resolves_by_target_text_and_detects_ambiguity(self) -> None:
        single = resolve_phone_target(SAMPLE_PHONE_SCREEN, {"target_text": "search in mail"})
        self.assertTrue(single["ok"])
        self.assertEqual(single["element_id"], "p1")

        ambig = resolve_phone_target(SAMPLE_PHONE_SCREEN, {"target_text": "Reply"})
        self.assertFalse(ambig["ok"])
        self.assertEqual(ambig["reason"], "ambiguous")
        self.assertEqual(len(ambig["candidates"]), 2)

        first_occ = resolve_phone_target(SAMPLE_PHONE_SCREEN, {"target_text": "Reply", "occurrence": 1})
        self.assertTrue(first_occ["ok"])
        self.assertEqual(first_occ["element_id"], "p4")


class Box2dTests(unittest.TestCase):
    EXPECTED_BOX = {"x": 216, "y": 240, "w": 216, "h": 480}

    def test_maps_y_first_normalized_box_to_screen_pixels(self) -> None:
        result = box_2d_to_screen_box([100, 200, 300, 400], 1080, 2400)
        self.assertTrue(result["ok"])
        self.assertEqual(result["box"], self.EXPECTED_BOX)
        self.assertEqual((result["x"], result["y"]), (324, 480))
        self.assertEqual(result["box_2d"], [100, 200, 300, 400])

    def test_swapped_corners_and_json_string_are_accepted(self) -> None:
        swapped = box_2d_to_screen_box([300, 400, 100, 200], 1080, 2400)
        as_text = box_2d_to_screen_box("[100, 200, 300, 400]", 1080, 2400)
        self.assertEqual(swapped["box"], self.EXPECTED_BOX)
        self.assertEqual(as_text["box"], self.EXPECTED_BOX)

    def test_rejects_malformed_out_of_range_and_empty_boxes(self) -> None:
        self.assertEqual(box_2d_to_screen_box([100, 200, 300], 1080, 2400)["reason"], "bad_box")
        self.assertEqual(box_2d_to_screen_box([100, 200, 1300, 400], 1080, 2400)["reason"], "bad_box")
        self.assertEqual(box_2d_to_screen_box([True, 200, 300, 400], 1080, 2400)["reason"], "bad_box")
        self.assertEqual(box_2d_to_screen_box([100, 200, 100, 400], 1080, 2400)["reason"], "empty_box")
        self.assertEqual(box_2d_to_screen_box([100, 200, 300, 400], 0, 2400)["reason"], "no_screen_size")

    def test_full_screen_box_stays_on_screen(self) -> None:
        result = box_2d_to_screen_box([0, 0, 1000, 1000], 1080, 2400)
        self.assertTrue(result["ok"])
        self.assertLess(result["x"], 1080)
        self.assertLess(result["y"], 2400)

    def test_screen_size_from_snapshot(self) -> None:
        self.assertEqual(screen_size_from({"screen_width": 1080, "screen_height": 2400}), (1080, 2400))
        self.assertEqual(screen_size_from(SAMPLE_PHONE_SCREEN), (0, 0))
        self.assertEqual(screen_size_from(None), (0, 0))


if __name__ == "__main__":
    unittest.main()
