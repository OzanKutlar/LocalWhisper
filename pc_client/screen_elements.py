"""Pure logic for OCR screen snapshots: element ids, text matching, staleness and click points.

This module makes no Win32 calls, so it can be unit tested on any OS.
"""

from __future__ import annotations

import difflib
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_ELEMENTS = 150
MAX_RAW_LINES = 400
MAX_SNAPSHOTS = 3
SNAPSHOT_TTL_S = 120.0
RECT_TOLERANCE_PX = 4
FUZZY_THRESHOLD = 0.75
PHRASE_MATCH_SCORE = 0.9
MAX_FUZZY_SCORE = 0.89
MIN_CANDIDATE_SCORE = 0.3
MAX_CANDIDATES = 5
MAX_ELEMENT_TEXT = 120
MAX_CLICK_OFFSET_PX = 400
MAX_ELEMENT_WORDS_SCANNED = 40


@dataclass(frozen=True)
class ScreenElement:
    """One OCR text line with its bounding box in physical screen pixels."""

    element_id: str
    text: str
    x: int
    y: int
    w: int
    h: int

    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def compact(self) -> str:
        return f'{self.element_id} "{self.text}" ({self.x},{self.y} {self.w}x{self.h})'

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }


@dataclass
class ScreenSnapshot:
    """OCR elements captured from one window (or the whole screen) at one moment."""

    snapshot_id: str
    hwnd: int
    window_title: str
    window_rect: dict[str, int] | None
    elements: list[ScreenElement]
    captured_at: float = field(default_factory=time.monotonic)
    invalidated: bool = False

    def find(self, element_id: str) -> ScreenElement | None:
        wanted = str(element_id).strip().lower()
        return next((e for e in self.elements if e.element_id == wanted), None)

    def compact(self, limit: int = MAX_ELEMENTS) -> str:
        bounded = max(0, min(limit, MAX_ELEMENTS))
        age_s = int(max(0.0, time.monotonic() - self.captured_at))
        header = (
            f'snapshot {self.snapshot_id} · window "{self.window_title}" · '
            f"{len(self.elements)} elements · age {age_s}s"
        )
        lines = [header] + [element.compact() for element in self.elements[:bounded]]
        if not self.elements:
            lines.append("(no text found)")
        hidden = len(self.elements) - bounded
        if hidden > 0:
            lines.append(f"… {hidden} more elements (inspect a single window to see them)")
        return "\n".join(lines)


def parse_ocr_lines(raw_lines: Any) -> list[dict[str, Any]]:
    """Validates OCR line dicts from screen_capture.ps1; malformed entries are dropped."""
    if not isinstance(raw_lines, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in raw_lines[:MAX_RAW_LINES]:
        line = _parse_ocr_line(item)
        if line is not None:
            parsed.append(line)
    return parsed


def _parse_ocr_line(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    text = " ".join(str(item.get("text", "")).split())[:MAX_ELEMENT_TEXT]
    if not text:
        return None
    try:
        x, y, w, h = (int(item.get(key, 0)) for key in ("x", "y", "w", "h"))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return {"text": text, "x": x, "y": y, "w": w, "h": h}


def build_elements(lines: list[dict[str, Any]]) -> list[ScreenElement]:
    """Assigns e1..eN ids in OCR reading order, capped at MAX_ELEMENTS."""
    return [
        ScreenElement(
            element_id=f"e{index}",
            text=line["text"],
            x=line["x"],
            y=line["y"],
            w=line["w"],
            h=line["h"],
        )
        for index, line in enumerate(lines[:MAX_ELEMENTS], start=1)
    ]


class SnapshotCache:
    """Keeps the newest few snapshots so element ids stay valid across tool calls."""

    def __init__(self, max_snapshots: int = MAX_SNAPSHOTS, ttl_s: float = SNAPSHOT_TTL_S) -> None:
        if max_snapshots < 1:
            raise ValueError("max_snapshots must be at least 1")
        self.max_snapshots = max_snapshots
        self.ttl_s = ttl_s
        self._snapshots: list[ScreenSnapshot] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(
        self,
        hwnd: int,
        window_title: str,
        window_rect: dict[str, int] | None,
        elements: list[ScreenElement],
    ) -> ScreenSnapshot:
        with self._lock:
            snapshot = ScreenSnapshot(
                snapshot_id=f"s{next(self._ids)}",
                hwnd=int(hwnd),
                window_title=window_title,
                window_rect=window_rect,
                elements=list(elements),
            )
            self._snapshots.append(snapshot)
            del self._snapshots[: -self.max_snapshots]
            return snapshot

    def get(self, snapshot_id: str | None = None) -> ScreenSnapshot | None:
        """Snapshot by id, or the newest one when snapshot_id is empty."""
        with self._lock:
            if not snapshot_id:
                return self._snapshots[-1] if self._snapshots else None
            wanted = str(snapshot_id).strip().lower()
            return next((s for s in self._snapshots if s.snapshot_id == wanted), None)

    def invalidate_window(self, hwnd: int) -> None:
        """Marks every snapshot of this window outdated (after a click, type or scroll)."""
        with self._lock:
            for snapshot in self._snapshots:
                if snapshot.hwnd == int(hwnd):
                    snapshot.invalidated = True


def rects_match(a: dict[str, int], b: dict[str, int], tolerance: int = RECT_TOLERANCE_PX) -> bool:
    return all(
        abs(int(a.get(key, 0)) - int(b.get(key, 0))) <= tolerance
        for key in ("x", "y", "width", "height")
    )


def staleness_reason(
    snapshot: ScreenSnapshot,
    current_rect: dict[str, int] | None,
    now: float,
    ttl_s: float = SNAPSHOT_TTL_S,
) -> str | None:
    """Why the snapshot can no longer be trusted for clicking, or None when it is fresh."""
    sid = snapshot.snapshot_id
    if snapshot.invalidated:
        return (
            f"Snapshot {sid} is outdated because the screen changed after a click, type or scroll. "
            "Use the newest snapshot or call inspect_pc_screen again."
        )
    age_s = now - snapshot.captured_at
    if age_s > ttl_s:
        return f"Snapshot {sid} is {int(age_s)}s old (limit {int(ttl_s)}s). Call inspect_pc_screen again."
    if snapshot.hwnd == 0 or snapshot.window_rect is None:
        return None
    if current_rect is None:
        return f"The window '{snapshot.window_title}' from snapshot {sid} is no longer open."
    if not rects_match(snapshot.window_rect, current_rect):
        return (
            f"The window '{snapshot.window_title}' moved or resized since snapshot {sid}. "
            "Call inspect_pc_screen again."
        )
    return None


def normalize_text(text: str) -> str:
    return " ".join(str(text).lower().split())


def _best_window_ratio(element_words: list[str], query: str) -> float:
    """Best fuzzy ratio between the query and any run of the same number of element words."""
    window_len = max(1, len(query.split()))
    words = element_words[:MAX_ELEMENT_WORDS_SCANNED]
    best = 0.0
    for start in range(max(1, len(words) - window_len + 1)):
        window = " ".join(words[start : start + window_len])
        best = max(best, difflib.SequenceMatcher(None, window, query).ratio())
    return best


def score_text_match(element_text: str, query: str) -> float:
    """1.0 exact, 0.9 whole-phrase containment, otherwise a capped fuzzy ratio."""
    element = normalize_text(element_text)
    wanted = normalize_text(query)
    if not element or not wanted:
        return 0.0
    if element == wanted:
        return 1.0
    if f" {wanted} " in f" {element} ":
        return PHRASE_MATCH_SCORE
    return min(_best_window_ratio(element.split(), wanted), MAX_FUZZY_SCORE)


def resolve_text_target(
    elements: list[ScreenElement],
    query: str,
    occurrence: int | None = None,
) -> tuple[ScreenElement | None, list[ScreenElement], str]:
    """Returns (element, candidates, reason). Never guesses between equally good matches.

    reason is '' on success, otherwise 'no_match', 'ambiguous' or 'bad_occurrence'.
    """
    ranked = sorted(
        ((score_text_match(e.text, query), e) for e in elements),
        key=lambda pair: -pair[0],
    )
    matches = [(score, e) for score, e in ranked if score >= FUZZY_THRESHOLD]
    if not matches:
        near = [e for score, e in ranked if score >= MIN_CANDIDATE_SCORE]
        return None, near[:MAX_CANDIDATES], "no_match"

    top_score = matches[0][0]
    top = sorted(
        (e for score, e in matches if score >= top_score - 1e-9),
        key=lambda e: (e.y, e.x),
    )
    if occurrence is not None:
        if 1 <= occurrence <= len(top):
            return top[occurrence - 1], [], ""
        return None, top[:MAX_CANDIDATES], "bad_occurrence"
    if len(top) == 1:
        return top[0], [], ""
    return None, top[:MAX_CANDIDATES], "ambiguous"


def clamp_offset(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return 0
    return max(-MAX_CLICK_OFFSET_PX, min(offset, MAX_CLICK_OFFSET_PX))


def click_point(element: ScreenElement, offset_x: Any = 0, offset_y: Any = 0) -> tuple[int, int]:
    center_x, center_y = element.center()
    return center_x + clamp_offset(offset_x), center_y + clamp_offset(offset_y)
