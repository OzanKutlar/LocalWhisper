"""Win32 clipboard text read/write for LocalWhisper PC client."""

from __future__ import annotations

import ctypes
import os
import time
from typing import Any

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_ATTEMPTS = 6
OPEN_RETRY_DELAY_S = 0.04
DEFAULT_MAX_READ_CHARS = 16_000
MAX_WRITE_CHARS = 200_000


def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def _configure_win32_clipboard() -> tuple[Any, Any]:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return user32, kernel32


def _open_clipboard_with_retry(user32: Any) -> bool:
    for attempt in range(OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            return True
        if attempt + 1 < OPEN_ATTEMPTS:
            time.sleep(OPEN_RETRY_DELAY_S)
    return False


def read_clipboard_text(max_chars: int = DEFAULT_MAX_READ_CHARS) -> dict[str, Any]:
    """Reads Unicode text from the Windows clipboard."""
    if not _is_windows():
        raise RuntimeError("Clipboard reading is only supported on Windows")
    bounded = max(1, min(int(max_chars), MAX_WRITE_CHARS))
    user32, kernel32 = _configure_win32_clipboard()
    if not _open_clipboard_with_retry(user32):
        raise RuntimeError("Clipboard is currently locked by another application")
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return {"ok": True, "text": "", "length": 0, "truncated": False, "empty": True}
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return {"ok": True, "text": "", "length": 0, "truncated": False, "empty": True}
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return {"ok": True, "text": "", "length": 0, "truncated": False, "empty": True}
        try:
            full_text = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()

    truncated = len(full_text) > bounded
    return {
        "ok": True,
        "text": full_text[:bounded],
        "length": len(full_text),
        "truncated": truncated,
        "empty": len(full_text) == 0,
    }


def write_clipboard_text(text: str) -> dict[str, Any]:
    """Writes Unicode text to the Windows clipboard."""
    if not isinstance(text, str):
        raise ValueError("Clipboard text must be a string")
    if len(text) > MAX_WRITE_CHARS:
        raise ValueError(f"Clipboard text exceeds {MAX_WRITE_CHARS} characters")
    if not _is_windows():
        raise RuntimeError("Clipboard writing is only supported on Windows")

    user32, kernel32 = _configure_win32_clipboard()
    encoded = (text + "\x00").encode("utf-16-le")
    if not _open_clipboard_with_retry(user32):
        raise RuntimeError("Clipboard is currently locked by another application")
    hglobal = None
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("Could not clear the clipboard")
        hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not hglobal:
            raise RuntimeError("Could not allocate clipboard memory")
        ptr = kernel32.GlobalLock(hglobal)
        if not ptr:
            raise RuntimeError("Could not lock clipboard memory")
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(hglobal)
        if not user32.SetClipboardData(CF_UNICODETEXT, hglobal):
            raise RuntimeError("SetClipboardData failed")
        hglobal = None  # Ownership transferred to Windows.
    finally:
        if hglobal:
            kernel32.GlobalFree(hglobal)
        user32.CloseClipboard()
    return {"ok": True, "length": len(text)}
