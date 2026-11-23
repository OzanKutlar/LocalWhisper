"""Unit tests for server/phone_elements.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phone_elements import (  # noqa: E402
    format_phone_screen_payload,
    parse_phone_elements,
    resolve_phone_target,
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


if __name__ == "__main__":
    unittest.main()
