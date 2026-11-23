"""Pure logic for Android phone screen snapshots: p1..pN element ids, compact view and matching."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

MAX_PHONE_ELEMENTS = 120
MAX_ELEMENT_TEXT = 120
MAX_CLICK_OFFSET_PX = 400
FUZZY_THRESHOLD = 0.75
PHRASE_MATCH_SCORE = 0.9
MAX_FUZZY_SCORE = 0.89
MIN_CANDIDATE_SCORE = 0.3
MAX_CANDIDATES = 5
MAX_WORDS_SCANNED = 40


@dataclass(frozen=True)
class PhoneElement:
    """One UI element from the Android accessibility snapshot with its screen box."""

    element_id: str
    text: str
    content_desc: str
    hint: str
    role: str
    view_id: str
    clickable: bool
    editable: bool
    scrollable: bool
    focused: bool
    x: int
    y: int
    w: int
    h: int

    def display_label(self) -> str:
        for candidate in (self.text, self.content_desc, self.hint, self.view_id, self.role):
            if candidate:
                return candidate
        return "element"

    def searchable_texts(self) -> list[str]:
        seen: list[str] = []
        for val in (self.text, self.content_desc, self.hint, self.view_id):
            if val and val not in seen:
                seen.append(val)
        return seen

    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def compact(self) -> str:
        tags: list[str] = []
        if self.role:
            tags.append(self.role)
        if self.clickable:
            tags.append("clickable")
        if self.editable:
            tags.append("editable")
        if self.scrollable:
            tags.append("scrollable")
        if self.focused:
            tags.append("focused")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        return f'{self.element_id} "{self.display_label() secure_quote}" ({self.x},{self.y} {self.w}x{self.h})'.replace(
            f'"{self.display_label() secure_quote}"',
            f'"{self.display_label()}"{tag_str}',
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "text": self.text,
            "content_desc": self.content_desc,
            "hint": self.hint,
            "role": self.role,
            "view_id": self.view_id,
            "clickable": self.clickable,
            "editable": self.editable,
            "scrollable": self.scrollable,
            "focused": self.focused,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }


def parse_phone_elements(screen: dict[str, Any] | None) -> list[PhoneElement]:
    """Extracts p1..pN PhoneElement objects from a phone_screen JSON dictionary."""
    if not isinstance(screen, dict):
        return []
    raw_list = screen.get("ui_elements")
    if not isinstance(raw_list, list):
        return []
    elements: list[PhoneElement] = []
    for idx, item in enumerate(raw_list[:MAX_PHONE_ELEMENTS], start=1):
        parsed = _parse_single_element(item, idx)
        if parsed is not None:
            elements.append(parsed)
    return elements


def _parse_single_element(item: Any, index: int) -> PhoneElement | None:
    if not isinstance(item, dict):
        return None
    raw_id = str(item.get("element_id", "") or "").strip().lower()
    element_id = raw_id if raw_id.startswith("p") else f"p{index}"
    text = _clean_str(item.get("text", ""))
    content_desc = _clean_str(item.get("content_desc", ""))
    hint = _clean_str(item.get("hint", ""))
    role = _clean_str(item.get("role", ""), limit=48)
    view_id = _clean_str(item.get("view_id", ""), limit=64)
    clickable = bool(item.get("clickable", False))
    editable = bool(item.get("editable", False))
    scrollable = bool(item.get("scrollable", False))
    focused = bool(item.get("focused", False))
    x, y, w, h = _extract_box(item)
    if w <= 0 or h <= 0:
        return None
    return PhoneElement(
        element_id=element_id,
        text=text,
        content_desc=content_desc,
        hint=hint,
        role=role,
        view_id=view_id,
        clickable=clickable,
        editable=editable,
        scrollable=scrollable,
        focused=focused,
        x=x,
        y=y,
        w=w,
        h=h,
    )


def _extract_box(item: dict[str, Any]) -> tuple[int, int, int, int]:
    bounds = item.get("bounds")
    if isinstance(bounds, dict):
        try:
            left = int(bounds.get("left", 0))
            top = int(bounds.get("top", 0))
            right = int(bounds.get("right", 0))
            bottom = int(bounds.get("bottom", 0))
            return left, top, max(0, right - left), max(0, bottom - top)
        except (TypeError, ValueError):
            return 0, 0, 0, 0
    try:
        return (
            int(item.get("x", 0)),
            int(item.get("y", 0)),
            int(item.get("w", 0)),
            int(item.get("h", 0)),
        )
    except (TypeError, ValueError):
        return 0, 0, 0, 0


def _clean_str(val: Any, limit: int = MAX_ELEMENT_TEXT) -> str:
    return " ".join(str(val or "").split())[:limit]


def format_phone_screen_payload(screen: dict[str, Any] | None, query: str = "") -> dict[str, Any]:
    """Formats the phone screen into a compact LLM-friendly dictionary with p1..pN element lines."""
    if not isinstance(screen, dict) or not screen:
        return {
            "accessibility_enabled": False,
            "note": "No phone screen snapshot was provided in this request.",
        }
    if not screen.get("accessibility_enabled", False):
        return dict(screen)

    snapshot_id = str(screen.get("phone_snapshot_id", "") or "ps1")
    app_name = str(screen.get("app_name", "") or "Unknown")
    package_name = str(screen.get("package_name", "") or "")
    elements = parse_phone_elements(screen)
    clean_query = query.strip().lower()
    if clean_query:
        elements = [
            el
            for el in elements
            if clean_query in el.compact().lower()
            or any(clean_query in s.lower() for s in el.searchable_texts())
        ]

    header = f'phone_snapshot {snapshot_id} · app "{app_name}" ({package_name}) · {len(elements)} elements'
    lines = [header] + [el.compact() for el in elements]
    if not elements:
        lines.append("(no matching elements found)")

    payload: dict[str, Any] = {
        "accessibility_enabled": True,
        "phone_snapshot_id": snapshot_id,
        "app_name": app_name,
        "package_name": package_name,
        "element_count": len(elements),
        "elements": "\n".join(lines),
    }
    if "focused_input" in screen:
        payload["focused_input"] = screen["focused_input"]
    return payload


def normalize_text(text: str) -> str:
    return " ".join(str(text).lower().split())


def _best_window_ratio(words: list[str], query: str) -> float:
    window_len = max(1, len(query.split()))
    bounded = words[:MAX_WORDS_SCANNED]
    best = 0.0
    for start in range(max(1, len(bounded) - window_len + 1)):
        window = " ".join(bounded[start : start + window_len])
        best = max(best, difflib.SequenceMatcher(None, window, query).ratio())
    return best


def score_text_match(candidate_text: str, query: str) -> float:
    element = normalize_text(candidate_text)
    wanted = normalize_text(query)
    if not element or not wanted:
        return 0.0
    if element == wanted:
        return 1.0
    if f" {wanted} " in f" {element} ":
        return PHRASE_MATCH_SCORE
    return min(_best_window_ratio(element.split(), wanted), MAX_FUZZY_SCORE)


def score_phone_element(element: PhoneElement, query: str) -> float:
    scores = [score_text_match(text, query) for text in element.searchable_texts()]
    return max(scores) if scores else 0.0


def resolve_phone_text_target(
    elements: list[PhoneElement],
    query: str,
    occurrence: int | None = None,
) -> tuple[PhoneElement | None, list[PhoneElement], str]:
    """Matches query against phone elements; returns (element, candidates, reason)."""
    ranked = sorted(
        ((score_phone_element(el, query), el) for el in elements),
        key=lambda pair: -pair[0],
    )
    matches = [(score, el) for score, el in ranked if score >= FUZZY_THRESHOLD]
    if not matches:
        near = [el for score, el in ranked if score >= MIN_CANDIDATE_SCORE]
        return None, near[:MAX_CANDIDATES], "no_match"

    top_score = matches[0][0]
    top = sorted(
        (el for score, el in matches if score >= top_score - 1e-9),
        key=lambda el: (el.y, el.x),
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


def phone_click_point(element: PhoneElement, offset_x: Any = 0, offset_y: Any = 0) -> tuple[int, int]:
    cx, cy = element.center()
    return cx + clamp_offset(offset_x), cy + clamp_offset(offset_y)


def resolve_phone_target(screen: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolves element_id, target_text or x/y against the current phone_screen snapshot."""
    if not isinstance(screen, dict) or not screen.get("accessibility_enabled", False):
        return {
            "ok": False,
            "reason": "accessibility_disabled",
            "message": (
                "LocalWhisper typing Accessibility service is disabled or no phone screen snapshot "
                "is available. Enable 'Type on this phone' in LocalWhisper settings."
            ),
        }

    elements = parse_phone_elements(screen)
    snapshot_id = str(screen.get("phone_snapshot_id", "") or "ps1")
    app_name = str(screen.get("app_name", "") or "Phone")
    element_id = str(arguments.get("element_id", "") or "").strip().lower()
    target_text = str(arguments.get("target_text", "") or "").strip()

    if element_id:
        matched = next((el for el in elements if el.element_id == element_id), None)
        if matched is None:
            return {
                "ok": False,
                "reason": "unknown_element",
                "message": f"{element_id} is not in phone snapshot {snapshot_id}.",
                "candidates": [el.compact() for el in elements[:MAX_CANDIDATES]],
            }
        return _resolved_phone_element(snapshot_id, app_name, matched, arguments)

    if target_text:
        occ = _optional_int(arguments.get("occurrence"))
        matched, candidates, reason = resolve_phone_text_target(elements, target_text, occ)
        if matched is None:
            return {
                "ok": False,
                "reason": reason,
                "message": _phone_text_failure_message(reason, target_text),
                "phone_snapshot_id": snapshot_id,
                "candidates": [c.compact() for c in candidates],
            }
        return _resolved_phone_element(snapshot_id, app_name, matched, arguments)

    if arguments.get("x") is not None and arguments.get("y") is not None:
        raw_x = _optional_int(arguments.get("x"))
        raw_y = _optional_int(arguments.get("y"))
        if raw_x is None or raw_y is None:
            return {"ok": False, "reason": "bad_point", "message": "x and y must be integers."}
        x = raw_x + clamp_offset(arguments.get("offset_x", 0))
        y = raw_y + clamp_offset(arguments.get("offset_y", 0))
        return {
            "ok": True,
            "mode": "point",
            "phone_snapshot_id": snapshot_id,
            "element_id": "",
            "display_text": "",
            "window_title": app_name,
            "x": x,
            "y": y,
            "box": None,
        }

    return {
        "ok": False,
        "reason": "missing_target",
        "message": "Provide element_id (preferred, e.g. 'p2'), target_text, or x and y.",
    }


def _resolved_phone_element(
    snapshot_id: str,
    app_name: str,
    element: PhoneElement,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    x, y = phone_click_point(element, arguments.get("offset_x", 0), arguments.get("offset_y", 0))
    return {
        "ok": True,
        "mode": "element",
        "phone_snapshot_id": snapshot_id,
        "element_id": element.element_id,
        "display_text": element.display_label(),
        "window_title": app_name,
        "x": x,
        "y": y,
        "box": element.to_dict(),
    }


def _phone_text_failure_message(reason: str, text: str) -> str:
    if reason == "ambiguous":
        return (
            f"'{text}' appears several times on the phone screen. "
            "Retry with an element_id from candidates or occurrence (1 = topmost)."
        )
    if reason == "bad_occurrence":
        return f"occurrence is out of range for '{text}'. Pick one of the candidates."
    return f"Could not find '{text}' on the phone screen. Closest elements are in candidates."


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
