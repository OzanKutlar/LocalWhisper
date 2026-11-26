"""Pure multi-monitor window layout geometry (no Win32 calls; unit-testable on any OS)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_ARRANGEMENTS = 12
MIN_WINDOW_W = 160
MIN_WINDOW_H = 120
MAX_BORDER_COMPENSATION_PX = 40

NAMED_SLOTS = frozenset(
    {
        "full",
        "maximize",
        "minimize",
        "left",
        "right",
        "top",
        "bottom",
        "left_third",
        "center_third",
        "right_third",
        "left_two_thirds",
        "right_two_thirds",
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
        "custom",
    }
)

# (x_frac, y_frac, w_frac, h_frac) within the monitor work area.
_SLOT_FRACTIONS: dict[str, tuple[float, float, float, float]] = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "maximize": (0.0, 0.0, 1.0, 1.0),
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 0.5),
    "left_third": (0.0, 0.0, 1.0 / 3.0, 1.0),
    "center_third": (1.0 / 3.0, 0.0, 1.0 / 3.0, 1.0),
    "right_third": (2.0 / 3.0, 0.0, 1.0 / 3.0, 1.0),
    "left_two_thirds": (0.0, 0.0, 2.0 / 3.0, 1.0),
    "right_two_thirds": (1.0 / 3.0, 0.0, 2.0 / 3.0, 1.0),
    "top_left": (0.0, 0.0, 0.5, 0.5),
    "top_right": (0.5, 0.0, 0.5, 0.5),
    "bottom_left": (0.0, 0.5, 0.5, 0.5),
    "bottom_right": (0.5, 0.5, 0.5, 0.5),
    "center": (0.15, 0.1, 0.7, 0.8),
}


@dataclass(frozen=True)
class MonitorInfo:
    """Usable work area (excluding taskbar) of one display, numbered left-to-right from 1."""

    index: int
    name: str
    is_primary: bool
    work_x: int
    work_y: int
    work_w: int
    work_h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "is_primary": self.is_primary,
            "work_area": {
                "x": self.work_x,
                "y": self.work_y,
                "width": self.work_w,
                "height": self.work_h,
            },
        }


def sort_monitors_left_to_right(monitors: list[MonitorInfo]) -> list[MonitorInfo]:
    """Sorts monitors left-to-right (top-to-bottom on ties) and numbers them 1..N."""
    ordered = sorted(monitors, key=lambda m: (m.work_x, m.work_y, not m.is_primary))
    return [
        MonitorInfo(
            index=idx,
            name=mon.name,
            is_primary=mon.is_primary,
            work_x=mon.work_x,
            work_y=mon.work_y,
            work_w=mon.work_w,
            work_h=mon.work_h,
        )
        for idx, mon in enumerate(ordered, start=1)
    ]


def resolve_monitor(monitors: list[MonitorInfo], spec: Any) -> MonitorInfo:
    """Selects a monitor by 1-based index, 'primary'/'main', or name substring."""
    if not monitors:
        raise ValueError("No monitors detected")
    if spec is None or str(spec).strip() == "":
        return next((m for m in monitors if m.is_primary), monitors[0])
    if isinstance(spec, bool):
        raise ValueError("monitor must be a monitor number or 'primary'")
    if isinstance(spec, int):
        return _monitor_by_index(monitors, spec)
    clean = str(spec).strip().lower()
    if clean in ("primary", "main", "default"):
        return next((m for m in monitors if m.is_primary), monitors[0])
    if clean.isdigit():
        return _monitor_by_index(monitors, int(clean))
    if clean.startswith("monitor ") and clean[8:].strip().isdigit():
        return _monitor_by_index(monitors, int(clean[8:].strip()))
    for mon in monitors:
        if clean in mon.name.lower():
            return mon
    raise ValueError(f"Unknown monitor '{spec}'; available: 1..{len(monitors)} or 'primary'")


def _monitor_by_index(monitors: list[MonitorInfo], index: int) -> MonitorInfo:
    for mon in monitors:
        if mon.index == index:
            return mon
    raise ValueError(f"Monitor {index} does not exist (detected {len(monitors)} monitor(s))")


def normalize_slot(raw: Any) -> str:
    slot = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fullscreen": "full",
        "maximized": "maximize",
        "minimized": "minimize",
        "left_half": "left",
        "right_half": "right",
        "top_half": "top",
        "bottom_half": "bottom",
        "middle_third": "center_third",
        "centered": "center",
        "rect": "custom",
    }
    resolved = aliases.get(slot, slot)
    if resolved not in NAMED_SLOTS:
        raise ValueError(f"Unsupported layout slot '{raw}'")
    return resolved


def compute_slot_rect(
    monitor: MonitorInfo,
    slot: str,
    rect: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Computes the desired visible pixel rect inside the monitor's work area."""
    if monitor.work_w <= 0 or monitor.work_h <= 0:
        raise ValueError("Monitor work area is empty")
    clean_slot = normalize_slot(slot)
    if clean_slot == "minimize":
        raise ValueError("minimize slot has no rectangle")
    if clean_slot == "custom":
        return _custom_rect(monitor, rect)
    fx, fy, fw, fh = _SLOT_FRACTIONS[clean_slot]
    left = monitor.work_x + int(round(monitor.work_w * fx))
    top = monitor.work_y + int(round(monitor.work_h * fy))
    right = monitor.work_x + int(round(monitor.work_w * (fx + fw)))
    bottom = monitor.work_y + int(round(monitor.work_h * (fy + fh)))
    return {
        "x": left,
        "y": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
    }


def _custom_rect(monitor: MonitorInfo, rect: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(rect, dict):
        raise ValueError("slot='custom' requires a 'rect' object with x, y, width, height")
    try:
        rx = float(rect["x"])
        ry = float(rect["y"])
        rw = float(rect["width"])
        rh = float(rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("rect must provide numeric x, y, width and height") from exc
    if rw <= 0 or rh <= 0:
        raise ValueError("rect width and height must be positive")

    # Values in 0..1 are treated as fractions of the monitor work area.
    if 0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0 and rw <= 1.0 and rh <= 1.0:
        px = monitor.work_x + int(round(rx * monitor.work_w))
        py = monitor.work_y + int(round(ry * monitor.work_h))
        pw = int(round(rw * monitor.work_w))
        ph = int(round(rh * monitor.work_h))
    else:
        px = int(round(rx))
        py = int(round(ry))
        pw = int(round(rw))
        ph = int(round(rh))

    pw = max(min(MIN_WINDOW_W, monitor.work_w), min(pw, monitor.work_w))
    ph = max(min(MIN_WINDOW_H, monitor.work_h), min(ph, monitor.work_h))
    px = max(monitor.work_x, min(px, monitor.work_x + monitor.work_w - pw))
    py = max(monitor.work_y, min(py, monitor.work_y + monitor.work_h - ph))
    return {"x": px, "y": py, "width": pw, "height": ph}


def compensate_invisible_borders(
    target: dict[str, int],
    window_rect: dict[str, int] | None,
    extended_frame: dict[str, int] | None,
) -> dict[str, int]:
    """Expands target by the window's invisible DWM shadow borders so visible edges sit flush."""
    if not isinstance(window_rect, dict) or not isinstance(extended_frame, dict):
        return dict(target)
    left_pad = int(extended_frame["x"]) - int(window_rect["x"])
    top_pad = int(extended_frame["y"]) - int(window_rect["y"])
    right_pad = (int(window_rect["x"]) + int(window_rect["width"])) - (
        int(extended_frame["x"]) + int(extended_frame["width"])
    )
    bottom_pad = (int(window_rect["y"]) + int(window_rect["height"])) - (
        int(extended_frame["y"]) + int(extended_frame["height"])
    )
    pads = (left_pad, top_pad, right_pad, bottom_pad)
    if any(pad < 0 or pad > MAX_BORDER_COMPENSATION_PX for pad in pads):
        return dict(target)
    return {
        "x": int(target["x"]) - left_pad,
        "y": int(target["y"]) - top_pad,
        "width": int(target["width"]) + left_pad + right_pad,
        "height": int(target["height"]) + top_pad + bottom_pad,
    }


def find_window_monitor(window_rect: dict[str, int] | None, monitors: list[MonitorInfo]) -> int:
    """Returns the 1-based index of the monitor overlapping window_rect the most."""
    if not monitors or not isinstance(window_rect, dict):
        return 1 if monitors else 0
    wx = int(window_rect.get("x", 0))
    wy = int(window_rect.get("y", 0))
    ww = max(0, int(window_rect.get("width", 0)))
    wh = max(0, int(window_rect.get("height", 0)))
    best_index = monitors[0].index
    best_area = -1
    for mon in monitors:
        ox = max(0, min(wx + ww, mon.work_x + mon.work_w) - max(wx, mon.work_x))
        oy = max(0, min(wy + wh, mon.work_y + mon.work_h) - max(wy, mon.work_y))
        area = ox * oy
        if area > best_area:
            best_area = area
            best_index = mon.index
    if best_area > 0:
        return best_index
    cx = wx + ww // 2
    cy = wy + wh // 2
    nearest = min(
        monitors,
        key=lambda m: (cx - (m.work_x + m.work_w // 2)) ** 2 + (cy - (m.work_y + m.work_h // 2)) ** 2,
    )
    return nearest.index
