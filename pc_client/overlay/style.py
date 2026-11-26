"""Shared sizing for the PC Jarvis overlay: one UI scale factor applied to every size."""

from __future__ import annotations

import math
from typing import Any

DEFAULT_UI_SCALE = 1.2
MIN_UI_SCALE = 0.8
MAX_UI_SCALE = 2.0
FONT_FAMILY = "Segoe UI"

_ui_scale = DEFAULT_UI_SCALE


def clamp_ui_scale(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return DEFAULT_UI_SCALE
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_UI_SCALE
    if not math.isfinite(number):
        return DEFAULT_UI_SCALE
    return max(MIN_UI_SCALE, min(number, MAX_UI_SCALE))


def set_ui_scale(value: Any) -> float:
    """Sets the overlay scale; call before any overlay widget is built."""
    global _ui_scale
    _ui_scale = clamp_ui_scale(value)
    return _ui_scale


def ui_scale() -> float:
    return _ui_scale


def px(value: float) -> int:
    """A design size in pixels multiplied by the UI scale; never smaller than 1."""
    return max(1, int(round(float(value) * _ui_scale)))
