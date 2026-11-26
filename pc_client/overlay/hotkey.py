"""Global Win32 hotkey parser and listener thread for summoning the PC Jarvis popup."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import threading
from typing import Callable

LOGGER = logging.getLogger("localwhisper.pc_hotkey")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0x4C57
MAX_MESSAGE_LOOP_ITERATIONS = 10_000_000

_MODIFIER_ALIASES: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
}

_NAMED_VKS: dict[str, int] = {
    "space": 0x20,
    "spacebar": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "tilde": 0xC0,
    "backquote": 0xC0,
    "`": 0xC0,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parses a hotkey string like 'Ctrl+Alt+Space' into (win32_modifiers, virtual_key_code)."""
    parts = [p.strip() for p in str(spec or "").replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("Hotkey specification must not be empty")
    modifiers = MOD_NOREPEAT
    key_vk: int | None = None
    for part in parts:
        lower = part.lower()
        if lower in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[lower]
            continue
        if key_vk is not None:
            raise ValueError(f"Hotkey '{spec}' contains multiple non-modifier keys")
        if lower in _NAMED_VKS:
            key_vk = _NAMED_VKS[lower]
        elif len(part) == 1 and part.isalnum():
            key_vk = ord(part.upper())
        else:
            raise ValueError(f"Unsupported key '{part}' in hotkey '{spec}'")
    if key_vk is None:
        raise ValueError(f"Hotkey '{spec}' must include a non-modifier key (e.g. 'Ctrl+Alt+Space')")
    return modifiers, key_vk


class GlobalHotkeyListener:
    """Registers a system-wide hotkey on a dedicated Win32 message thread."""

    def __init__(self, hotkey_spec: str, on_pressed: Callable[[], None]) -> None:
        self.hotkey_spec = hotkey_spec
        self._modifiers, self._vk = parse_hotkey(hotkey_spec)
        self._on_pressed = on_pressed
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._ready = threading.Event()
        self.registered = False

    def start(self) -> bool:
        if os.name != "nt" or not hasattr(ctypes, "windll"):
            return False
        if self._thread is not None:
            return self.registered
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, name="LocalWhisper-Hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.registered

    def stop(self) -> None:
        tid = self._thread_id
        if tid and os.name == "nt" and hasattr(ctypes, "windll"):
            try:
                ctypes.windll.user32.PostThreadMessageW(wintypes.DWORD(tid), WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, self._modifiers, self._vk))
        self.registered = ok
        self._ready.set()
        if not ok:
            LOGGER.warning("Could not register global hotkey '%s' (already in use?)", self.hotkey_spec)
            return
        LOGGER.info("Registered global hotkey: %s", self.hotkey_spec)
        msg = wintypes.MSG()
        try:
            for _ in range(MAX_MESSAGE_LOOP_ITERATIONS):
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0 or msg.message == WM_QUIT:
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    try:
                        self._on_pressed()
                    except Exception as exc:
                        LOGGER.warning("Hotkey callback error: %s", exc)
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered = False
