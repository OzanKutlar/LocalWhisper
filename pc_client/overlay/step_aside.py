"""Step-aside policy and Win32 focus/capture helpers for the PC popup.

When the agent acts on this PC (or inspects its screen), the popup steps out of the way and
restores focus to the window that was active before the popup opened, then returns after a
600 ms grace period so back-to-back tool calls do not flicker.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
import time
from typing import Callable

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
RESTORE_GRACE_S = 0.6
FOCUS_SETTLE_S = 0.15

# PC tools that inspect the screen or send input to the foreground window.
STEP_ASIDE_TOOLS = frozenset(
    {
        "inspect_pc_screen",
        "capture_pc_screenshot",
        "prepare_click",
        "click_screen_element",
        "type_into_element",
        "scroll_pc",
        "move_pc_mouse",
        "execute_pc_ahk_sequence",
        "manage_pc_app",
        "arrange_pc_windows",
    }
)


def requires_step_aside(tool_name: str) -> bool:
    return str(tool_name or "").strip() in STEP_ASIDE_TOOLS


class StepAsideState:
    """Pure thread-safe state machine tracking whether the overlay should hide and restore."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.overlay_visible: bool = False
        self.hidden_by_agent: bool = False
        self.work_depth: int = 0
        self.remembered_hwnd: int = 0

    def on_overlay_shown(self, previous_hwnd: int = 0) -> None:
        with self._lock:
            self.overlay_visible = True
            self.hidden_by_agent = False
            if previous_hwnd > 0:
                self.remembered_hwnd = int(previous_hwnd)

    def on_user_hid_overlay(self) -> None:
        with self._lock:
            self.overlay_visible = False
            self.hidden_by_agent = False

    def begin_pc_work(self) -> bool:
        """Marks the start of local PC work; returns True if the visible overlay must hide now."""
        with self._lock:
            self.work_depth += 1
            if self.overlay_visible:
                self.overlay_visible = False
                self.hidden_by_agent = True
                return True
            return False

    def end_pc_work(self) -> bool:
        """Marks the end of local PC work; returns True when a restore timer should be scheduled."""
        with self._lock:
            self.work_depth = max(0, self.work_depth - 1)
            return self.work_depth == 0 and self.hidden_by_agent

    def should_restore_now(self) -> bool:
        """True when the grace timer fires and no new PC work or user minimize happened."""
        with self._lock:
            if not self.hidden_by_agent or self.work_depth > 0 or self.overlay_visible:
                return False
            self.hidden_by_agent = False
            self.overlay_visible = True
            return True


def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def exclude_hwnd_from_capture(hwnd: int) -> bool:
    """Marks a top-level HWND so Windows screen capture skips it completely."""
    if hwnd <= 0 or not _is_windows():
        return False
    try:
        return bool(
            ctypes.windll.user32.SetWindowDisplayAffinity(
                wintypes.HWND(int(hwnd)),
                wintypes.DWORD(WDA_EXCLUDEFROMCAPTURE),
            )
        )
    except Exception:
        return False


def current_foreground_hwnd(excluded_hwnds: set[int]) -> int:
    if not _is_windows():
        return 0
    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    return 0 if hwnd in excluded_hwnds else hwnd


def restore_foreground_hwnd(hwnd: int) -> bool:
    if hwnd <= 0 or not _is_windows():
        return False
    user32 = ctypes.windll.user32
    handle = wintypes.HWND(int(hwnd))
    if not user32.IsWindow(handle):
        return False
    try:
        return bool(user32.SetForegroundWindow(handle))
    except Exception:
        return False


class StepAsideCoordinator:
    """Coordinates hiding the Qt popup on the UI thread before local PC tools run."""

    def __init__(
        self,
        hide_on_ui: Callable[[], None],
        restore_on_ui: Callable[[], None],
        grace_s: float = RESTORE_GRACE_S,
        settle_s: float = FOCUS_SETTLE_S,
    ) -> None:
        self.state = StepAsideState()
        self._hide_on_ui = hide_on_ui
        self._restore_on_ui = restore_on_ui
        self._grace_s = grace_s
        self._settle_s = settle_s
        self._timer_lock = threading.Lock()
        self._restore_timer: threading.Timer | None = None

    def cancel_restore_timer(self) -> None:
        with self._timer_lock:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
                self._restore_timer = None

    def run_with_step_aside(self, fn: Callable[[], object]) -> object:
        """Hides the popup and restores the target window before running fn, then schedules restore."""
        self.cancel_restore_timer()
        should_hide = self.state.begin_pc_work()
        try:
            if should_hide:
                self._hide_on_ui()
                restore_foreground_hwnd(self.state.remembered_hwnd)
                if self._settle_s > 0:
                    time.sleep(self._settle_s)
            return fn()
        finally:
            if self.state.end_pc_work():
                self._schedule_restore()

    def _schedule_restore(self) -> None:
        with self._timer_lock:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
            if self._grace_s <= 0:
                self._on_timer_fired()
                return
            timer = threading.Timer(self._grace_s, self._on_timer_fired)
            timer.daemon = True
            self._restore_timer = timer
            timer.start()

    def _on_timer_fired(self) -> None:
        if self.state.should_restore_now():
            self._restore_on_ui()
