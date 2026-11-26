"""Win32 multi-monitor enumeration and window tiling for LocalWhisper PC client."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from typing import Any, Callable

from layout_math import (
    MAX_ARRANGEMENTS,
    MonitorInfo,
    compensate_invisible_borders,
    compute_slot_rect,
    find_window_monitor,
    normalize_slot,
    resolve_monitor,
    sort_monitors_left_to_right,
)

MONITORINFOF_PRIMARY = 0x00000001
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
ERROR_ACCESS_DENIED = 5
MAX_MONITORS = 16


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def enumerate_monitors() -> list[MonitorInfo]:
    """Returns all active monitors numbered left-to-right from 1."""
    if not _is_windows():
        return []
    user32 = ctypes.windll.user32
    found: list[MonitorInfo] = []
    cb_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def _callback(hmon: int, _hdc: int, _lprect: Any, _lparam: int) -> bool:
        if len(found) >= MAX_MONITORS:
            return False
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        work = info.rcWork
        found.append(
            MonitorInfo(
                index=0,
                name=info.szDevice.strip() or f"Monitor {len(found) + 1}",
                is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                work_x=int(work.left),
                work_y=int(work.top),
                work_w=max(0, int(work.right - work.left)),
                work_h=max(0, int(work.bottom - work.top)),
            )
        )
        return True

    user32.EnumDisplayMonitors(0, 0, cb_type(_callback), 0)
    return sort_monitors_left_to_right(found)


def _get_window_rect(hwnd: int) -> dict[str, int] | None:
    if hwnd <= 0 or not _is_windows():
        return None
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def _get_extended_frame_bounds(hwnd: int) -> dict[str, int] | None:
    if hwnd <= 0 or not _is_windows():
        return None
    try:
        dwmapi = ctypes.windll.dwmapi
    except OSError:
        return None
    rect = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        return None
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def _move_window(hwnd: int, monitor: MonitorInfo, slot: str, rect: dict[str, Any] | None) -> tuple[bool, str]:
    if not _is_windows():
        return False, "Window arrangement is only supported on Windows"
    user32 = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    if not user32.IsWindow(handle):
        return False, "Window is no longer open"

    if slot == "minimize":
        user32.ShowWindow(handle, SW_MINIMIZE)
        return True, "minimized"

    if user32.IsIconic(handle) or user32.IsZoomed(handle):
        user32.ShowWindow(handle, SW_RESTORE)

    if slot == "maximize":
        # Move onto the target monitor first so SW_MAXIMIZE fills that display.
        if not _set_window_pos(handle, monitor.work_x + 40, monitor.work_y + 40, 800, 600):
            return False, _move_error_message()
        user32.ShowWindow(handle, SW_MAXIMIZE)
        return True, f"maximized on monitor {monitor.index}"

    desired = compute_slot_rect(monitor, slot, rect)
    adjusted = compensate_invisible_borders(desired, _get_window_rect(hwnd), _get_extended_frame_bounds(hwnd))
    if not _set_window_pos(handle, adjusted["x"], adjusted["y"], adjusted["width"], adjusted["height"]):
        return False, _move_error_message()
    return True, f"moved to {slot} on monitor {monitor.index}"


def _set_window_pos(handle: wintypes.HWND, x: int, y: int, width: int, height: int) -> bool:
    ctypes.set_last_error(0)
    ok = ctypes.windll.user32.SetWindowPos(
        handle,
        wintypes.HWND(0),
        int(x),
        int(y),
        int(width),
        int(height),
        SWP_NOZORDER | SWP_NOACTIVATE,
    )
    return bool(ok)


def _move_error_message() -> str:
    err = ctypes.get_last_error()
    if err == ERROR_ACCESS_DENIED:
        return "elevated_window: cannot move an administrator window from a standard process"
    return f"SetWindowPos failed (Win32 error {err})"


def run_arrange_pc_windows(
    arguments: dict[str, Any],
    enumerate_windows_fn: Callable[[], tuple[list[dict[str, Any]], str | None]],
    filter_windows_fn: Callable[[list[dict[str, Any]], str, bool], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Tiles or moves up to MAX_ARRANGEMENTS windows onto specified monitors and slots."""
    raw_items = arguments.get("arrangements")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("arrangements must be a non-empty list")
    if len(raw_items) > MAX_ARRANGEMENTS:
        raise ValueError(f"At most {MAX_ARRANGEMENTS} windows can be arranged per call")

    monitors = enumerate_monitors()
    if not monitors:
        raise RuntimeError("Could not detect any display monitors")
    all_windows, _fg = enumerate_windows_fn()

    outcomes: list[dict[str, Any]] = []
    moved_count = 0
    for item in raw_items:
        if not isinstance(item, dict):
            outcomes.append({"ok": False, "error": "Invalid arrangement item"})
            continue
        outcome = _apply_one_arrangement(item, all_windows, monitors, filter_windows_fn)
        if outcome.get("ok"):
            moved_count += 1
        outcomes.append(outcome)

    return {
        "ok": moved_count > 0,
        "moved_count": moved_count,
        "total": len(outcomes),
        "monitors": [m.to_dict() for m in monitors],
        "results": outcomes,
    }


def _apply_one_arrangement(
    item: dict[str, Any],
    all_windows: list[dict[str, Any]],
    monitors: list[MonitorInfo],
    filter_windows_fn: Callable[[list[dict[str, Any]], str, bool], list[dict[str, Any]]],
) -> dict[str, Any]:
    target = str(item.get("target", "") or "").strip()
    if not target:
        return {"ok": False, "target": "", "error": "target window name is required"}
    try:
        slot = normalize_slot(item.get("slot", ""))
        monitor = resolve_monitor(monitors, item.get("monitor"))
    except ValueError as exc:
        return {"ok": False, "target": target, "error": str(exc)}

    matches = filter_windows_fn(all_windows, target, True)
    if not matches:
        return {"ok": False, "target": target, "status": "not_found", "error": f"No window matching '{target}'"}

    win = matches[0]
    hwnd = int(win.get("_hwnd_int", 0) or 0)
    ok, detail = _move_window(hwnd, monitor, slot, item.get("rect"))
    return {
        "ok": ok,
        "target": target,
        "matched_title": str(win.get("title", "")),
        "process_name": str(win.get("process_name", "")),
        "monitor": monitor.index,
        "slot": slot,
        "detail": detail,
    }


def run_get_pc_layout(
    enumerate_windows_fn: Callable[[], tuple[list[dict[str, Any]], str | None]],
) -> dict[str, Any]:
    """Returns monitors and all visible non-minimized windows with their monitor and bounds."""
    monitors = enumerate_monitors()
    all_windows, _fg = enumerate_windows_fn()
    visible: list[dict[str, Any]] = []
    for win in all_windows:
        if win.get("state") == "minimized":
            continue
        bounds = win.get("bounds") if isinstance(win.get("bounds"), dict) else None
        mon_idx = find_window_monitor(bounds, monitors)
        visible.append(
            {
                "title": str(win.get("title", "")),
                "process_name": str(win.get("process_name", "")),
                "state": str(win.get("state", "")),
                "is_foreground": bool(win.get("is_foreground", False)),
                "monitor": mon_idx,
                "bounds": bounds,
            }
        )
    return {
        "ok": True,
        "monitors": [m.to_dict() for m in monitors],
        "windows": visible,
    }
