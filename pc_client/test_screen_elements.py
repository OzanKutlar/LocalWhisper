"""Unit tests for screen_elements. Run: python -m unittest discover -s pc_client -p "test_*.py" -v"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screen_elements import (  # noqa: E402
    MAX_CLICK_OFFSET_PX,
    MAX_ELEMENTS,
    ScreenElement,
    SnapshotCache,
    build_elements,
    clamp_offset,
    click_point,
    parse_ocr_lines,
    resolve_text_target,
    staleness_reason,
)

WINDOW_RECT = {"x": 0, "y": 0, "width": 800, "height": 600}


def make_elements(*specs: tuple[str, int, int]) -> list[ScreenElement]:
    return build_elements([{"text": t, "x": x, "y": y, "w": 60, "h": 18} for t, x, y in specs])


class ParsingTests(unittest.TestCase):
    def test_drops_malformed_lines_and_normalizes_whitespace(self) -> None:
        lines = parse_ocr_lines(
            [
                {"text": "  New   mail ", "x": 1, "y": 2, "w": 30, "h": 10},
                {"text": "", "x": 0, "y": 0, "w": 5, "h": 5},
                {"text": "bad", "x": "a", "y": 0, "w": 5, "h": 5},
                {"text": "zero", "x": 0, "y": 0, "w": 0, "h": 5},
                "junk",
            ]
        )
        self.assertEqual(lines, [{"text": "New mail", "x": 1, "y": 2, "w": 30, "h": 10}])

    def test_non_list_payload_returns_empty(self) -> None:
        self.assertEqual(parse_ocr_lines(None), [])
        self.assertEqual(parse_ocr_lines({"text": "x"}), [])

    def test_build_elements_assigns_sequential_ids_and_caps(self) -> None:
        many = [{"text": f"t{i}", "x": 0, "y": i, "w": 5, "h": 5} for i in range(MAX_ELEMENTS + 10)]
        elements = build_elements(many)
        self.assertEqual(len(elements), MAX_ELEMENTS)
        self.assertEqual(elements[0].element_id, "e1")
        self.assertEqual(elements[-1].element_id, f"e{MAX_ELEMENTS}")

    def test_compact_line_and_center(self) -> None:
        element = ScreenElement("e4", "Inbox", 14, 212, 58, 18)
        self.assertEqual(element.compact(), 'e4 "Inbox" (14,212 58x18)')
        self.assertEqual(element.center(), (43, 221))


class TextMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.elements = make_elements(
            ("Inbox", 10, 100),
            ("Sent Items", 10, 130),
            ("Reply", 400, 300),
            ("Reply", 400, 80),
            ("Meeting moved to 5pm", 300, 200),
        )

    def test_exact_match_is_case_insensitive(self) -> None:
        element, candidates, reason = resolve_text_target(self.elements, "inbox")
        self.assertEqual((element.element_id, candidates, reason), ("e1", [], ""))

    def test_phrase_inside_longer_line(self) -> None:
        element, _candidates, reason = resolve_text_target(self.elements, "moved to 5pm")
        self.assertEqual((element.element_id, reason), ("e5", ""))

    def test_fuzzy_match_tolerates_ocr_errors(self) -> None:
        element, _candidates, reason = resolve_text_target(self.elements, "lnbox")
        self.assertEqual((element.element_id, reason), ("e1", ""))

    def test_duplicate_text_is_ambiguous_and_sorted_top_to_bottom(self) -> None:
        element, candidates, reason = resolve_text_target(self.elements, "Reply")
        self.assertIsNone(element)
        self.assertEqual(reason, "ambiguous")
        self.assertEqual([c.element_id for c in candidates], ["e4", "e3"])

    def test_occurrence_picks_in_reading_order(self) -> None:
        first, _c, _r = resolve_text_target(self.elements, "Reply", occurrence=1)
        second, _c, _r = resolve_text_target(self.elements, "Reply", occurrence=2)
        self.assertEqual((first.element_id, second.element_id), ("e4", "e3"))
        missing, candidates, reason = resolve_text_target(self.elements, "Reply", occurrence=3)
        self.assertIsNone(missing)
        self.assertEqual(reason, "bad_occurrence")
        self.assertEqual(len(candidates), 2)

    def test_unknown_text_is_no_match(self) -> None:
        element, _candidates, reason = resolve_text_target(self.elements, "Calendar")
        self.assertIsNone(element)
        self.assertEqual(reason, "no_match")


class StalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = SnapshotCache()
        self.snapshot = self.cache.add(
            hwnd=42,
            window_title="Outlook",
            window_rect=dict(WINDOW_RECT),
            elements=make_elements(("Inbox", 10, 100)),
        )

    def test_fresh_snapshot_and_small_jitter_are_valid(self) -> None:
        now = time.monotonic()
        self.assertIsNone(staleness_reason(self.snapshot, dict(WINDOW_RECT), now))
        jittered = dict(WINDOW_RECT, x=3)
        self.assertIsNone(staleness_reason(self.snapshot, jittered, now))

    def test_moved_window_is_stale(self) -> None:
        moved = dict(WINDOW_RECT, x=50)
        self.assertIn("moved", staleness_reason(self.snapshot, moved, time.monotonic()))

    def test_closed_window_is_stale(self) -> None:
        self.assertIn("no longer open", staleness_reason(self.snapshot, None, time.monotonic()))

    def test_expired_snapshot_is_stale(self) -> None:
        self.snapshot.captured_at -= 500
        self.assertIn("old", staleness_reason(self.snapshot, dict(WINDOW_RECT), time.monotonic()))

    def test_invalidated_snapshot_is_stale(self) -> None:
        self.cache.invalidate_window(42)
        self.assertIn("outdated", staleness_reason(self.snapshot, dict(WINDOW_RECT), time.monotonic()))

    def test_full_screen_snapshot_without_window_is_valid(self) -> None:
        screen = self.cache.add(hwnd=0, window_title="Primary screen", window_rect=None, elements=[])
        self.assertIsNone(staleness_reason(screen, None, time.monotonic()))


class CacheTests(unittest.TestCase):
    def test_keeps_newest_snapshots_and_looks_up_by_id(self) -> None:
        cache = SnapshotCache(max_snapshots=3)
        for index in range(5):
            cache.add(hwnd=index, window_title=f"w{index}", window_rect=None, elements=[])
        self.assertIsNone(cache.get("s1"))
        self.assertEqual(cache.get("S3").window_title, "w2")
        self.assertEqual(cache.get(None).snapshot_id, "s5")

    def test_invalidate_only_affects_matching_window(self) -> None:
        cache = SnapshotCache()
        first = cache.add(hwnd=1, window_title="a", window_rect=None, elements=[])
        second = cache.add(hwnd=2, window_title="b", window_rect=None, elements=[])
        cache.invalidate_window(1)
        self.assertTrue(first.invalidated)
        self.assertFalse(second.invalidated)


class ClickPointTests(unittest.TestCase):
    def test_offsets_are_applied_and_clamped(self) -> None:
        element = ScreenElement("e4", "Inbox", 14, 212, 58, 18)
        self.assertEqual(click_point(element, 10, -5), (53, 216))
        self.assertEqual(clamp_offset(99999), MAX_CLICK_OFFSET_PX)
        self.assertEqual(clamp_offset("x"), 0)
        self.assertEqual(clamp_offset(True), 0)


if __name__ == "__main__":
    unittest.main()
