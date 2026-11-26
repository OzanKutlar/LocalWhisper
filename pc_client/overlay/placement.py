"""Pure geometry for placing the PC Jarvis popup below the mouse and snapping the corner pill."""

from __future__ import annotations

from dataclasses import dataclass

CURSOR_OFFSET_Y_PX = 18
EDGE_MARGIN_PX = 12
PILL_TOP_OFFSET_PX = 24


@dataclass(frozen=True)
class WorkArea:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom


def pick_work_area(cursor_x: int, cursor_y: int, areas: list[WorkArea]) -> WorkArea:
    """Returns the monitor work area containing the cursor, or the nearest one."""
    if not areas:
        return WorkArea(0, 0, 1920, 1080)
    for area in areas:
        if area.contains(cursor_x, cursor_y):
            return area
    return min(
        areas,
        key=lambda a: (cursor_x - (a.x + a.width // 2)) ** 2 + (cursor_y - (a.y + a.height // 2)) ** 2,
    )


def compute_popup_position(
    cursor_x: int,
    cursor_y: int,
    popup_width: int,
    popup_height: int,
    work_area: WorkArea,
    cursor_offset_y: int = CURSOR_OFFSET_Y_PX,
    edge_margin: int = EDGE_MARGIN_PX,
) -> tuple[int, int]:
    """Places the popup centered horizontally on the cursor and just below it.

    Flips above the cursor when there is not enough room below inside work_area.
    """
    margin = max(0, int(edge_margin))
    safe_w = max(1, min(int(popup_width), max(1, work_area.width - 2 * margin)))
    safe_h = max(1, min(int(popup_height), max(1, work_area.height - 2 * margin)))

    min_x = work_area.x + margin
    max_x = max(min_x, work_area.right - safe_w - margin)
    target_x = max(min_x, min(int(cursor_x) - safe_w // 2, max_x))

    min_y = work_area.y + margin
    max_y = max(min_y, work_area.bottom - safe_h - margin)
    below_y = int(cursor_y) + max(0, int(cursor_offset_y))
    above_y = int(cursor_y) - max(0, int(cursor_offset_y)) - safe_h

    if below_y + safe_h <= work_area.bottom - margin:
        target_y = max(min_y, below_y)
    elif above_y >= min_y:
        target_y = min(above_y, max_y)
    else:
        target_y = max(min_y, min(below_y, max_y))
    return target_x, target_y


def compute_initial_pill_position(
    pill_width: int,
    pill_height: int,
    work_area: WorkArea,
    edge_margin: int = EDGE_MARGIN_PX,
    top_offset: int = PILL_TOP_OFFSET_PX,
) -> tuple[int, int]:
    margin = max(0, int(edge_margin))
    x = max(work_area.x + margin, work_area.right - max(1, int(pill_width)) - margin)
    y = min(
        max(work_area.y + margin, work_area.y + max(0, int(top_offset))),
        max(work_area.y + margin, work_area.bottom - max(1, int(pill_height)) - margin),
    )
    return x, y


def snap_pill_to_edge(
    current_x: int,
    current_y: int,
    pill_width: int,
    pill_height: int,
    work_area: WorkArea,
    edge_margin: int = EDGE_MARGIN_PX,
) -> tuple[int, int]:
    """Snaps a dragged corner pill to the nearest left or right edge of work_area."""
    margin = max(0, int(edge_margin))
    min_x = work_area.x + margin
    max_x = max(min_x, work_area.right - max(1, int(pill_width)) - margin)
    center_x = int(current_x) + max(1, int(pill_width)) // 2
    snapped_x = min_x if center_x < (work_area.x + work_area.width // 2) else max_x

    min_y = work_area.y + margin
    max_y = max(min_y, work_area.bottom - max(1, int(pill_height)) - margin)
    clamped_y = max(min_y, min(int(current_y), max_y))
    return snapped_x, clamped_y
