"""PC-side agent tool implementations for LocalWhisper (AHK macros, apps, media, URLs)."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

CLIENT_DIR = Path(__file__).resolve().parent
WINDOW_AHK_SCRIPT = CLIENT_DIR / "window_action.ahk"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MAX_MACRO_STEPS = 25
MAX_STEP_WAIT_MS = 5000
MAX_VOLUME_STEPS = 20
WINDOW_AHK_TIMEOUT_S = 6

MEDIA_ACTION_TOKENS: dict[str, str] = {
    "play_pause": "{Media_Play_Pause}",
    "next_track": "{Media_Next}",
    "prev_track": "{Media_Prev}",
    "stop": "{Media_Stop}",
    "mute": "{Volume_Mute}",
}

APP_LAUNCH_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "taskmgr": "taskmgr.exe",
    "terminal": "wt.exe",
    "wt": "wt.exe",
    "code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "msedge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify",
    "discord": "discord",
}

# Window title substrings for common apps when focusing/closing
WINDOW_TITLE_HINTS: dict[str, str] = {
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "chrome": "Chrome",
    "edge": "Edge",
    "msedge": "Edge",
    "notepad": "Notepad",
    "terminal": "Terminal",
    "wt": "Terminal",
}


def dispatch_pc_tool(
    tool_name: str,
    arguments: dict[str, Any],
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    ahk_sender: Callable[[Path, str, str, bool], None],
) -> dict[str, Any]:
    """Executes a named PC tool and returns a JSON-serializable result dictionary."""
    if tool_name == "execute_pc_ahk_sequence":
        return run_ahk_sequence(arguments, ahk_exe, send_mode, human_typing, ahk_sender)
    if tool_name == "control_pc_media":
        return run_media_control(arguments, ahk_exe, send_mode, ahk_sender)
    if tool_name == "open_pc_url":
        return run_open_url(arguments)
    if tool_name == "manage_pc_app":
        return run_manage_app(arguments, ahk_exe)
    raise ValueError(f"Unsupported PC tool: {tool_name}")


def run_ahk_sequence(
    arguments: dict[str, Any],
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    ahk_sender: Callable[[Path, str, str, bool], None],
) -> dict[str, Any]:
    """Executes an ordered sequence of AutoHotkey v1.1 keystroke steps with bounded pauses."""
    raw_steps = arguments.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps must be a non-empty list")
    if len(raw_steps) > MAX_MACRO_STEPS:
        raise ValueError(f"Macro exceeds maximum of {MAX_MACRO_STEPS} steps")

    executed = 0
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        text = str(step.get("text", ""))
        raw_wait = step.get("wait_ms", 150)
        try:
            wait_ms = int(raw_wait)
        except (TypeError, ValueError):
            wait_ms = 150
        clamped_wait_ms = max(0, min(wait_ms, MAX_STEP_WAIT_MS))

        if text:
            ahk_sender(ahk_exe, text, send_mode, human_typing)
            executed += 1
        if clamped_wait_ms > 0:
            time.sleep(clamped_wait_ms / 1000.0)

    if executed == 0:
        raise ValueError("Macro sequence contained no non-empty text steps")
    return {"ok": True, "steps_executed": executed}


def build_media_ahk_token(action: str, steps: int = 2) -> str:
    """Maps a media/volume action name to an AutoHotkey v1.1 key token."""
    clean_action = action.strip().lower()
    if clean_action in MEDIA_ACTION_TOKENS:
        return MEDIA_ACTION_TOKENS[clean_action]

    clamped_steps = max(1, min(int(steps), MAX_VOLUME_STEPS))
    if clean_action == "volume_up":
        return f"{{Volume_Up {clamped_steps}}}"
    if clean_action == "volume_down":
        return f"{{Volume_Down {clamped_steps}}}"
    raise ValueError(f"Unsupported media action: {action}")


def run_media_control(
    arguments: dict[str, Any],
    ahk_exe: Path,
    send_mode: str,
    ahk_sender: Callable[[Path, str, str, bool], None],
) -> dict[str, Any]:
    action = str(arguments.get("action", "")).strip()
    raw_steps = arguments.get("steps", 2)
    try:
        steps = int(raw_steps)
    except (TypeError, ValueError):
        steps = 2
    token = build_media_ahk_token(action, steps)
    ahk_sender(ahk_exe, token, send_mode, False)
    return {"ok": True, "action": action, "token": token}


def validate_http_url(url: str) -> str:
    """Validates that the given string is an HTTP or HTTPS URL."""
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL must not be empty")
    if "://" not in cleaned:
        cleaned = "https://" + cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid HTTP/HTTPS URL: {url}")
    return cleaned


def run_open_url(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_url = str(arguments.get("url", ""))
    valid_url = validate_http_url(raw_url)
    opened = webbrowser.open(valid_url)
    return {"ok": bool(opened), "url": valid_url}


def run_manage_app(arguments: dict[str, Any], ahk_exe: Path) -> dict[str, Any]:
    action = str(arguments.get("action", "")).strip().lower()
    target = str(arguments.get("target", "")).strip()

    if action == "lock_pc":
        if os.name == "nt" and hasattr(ctypes, "windll"):
            ctypes.windll.user32.LockWorkStation()
            return {"ok": True, "action": "lock_pc"}
        raise RuntimeError("Workstation locking is only supported on Windows")

    if not target:
        raise ValueError(f"'target' is required for action '{action}'")

    if action in ("focus_window", "close_window"):
        ahk_mode = "focus" if action == "focus_window" else "close"
        return _run_window_ahk(ahk_exe, ahk_mode, target)

    if action == "launch_app":
        return _launch_application(target)

    raise ValueError(f"Unsupported manage_pc_app action: {action}")


def _run_window_ahk(ahk_exe: Path, mode: str, target: str) -> dict[str, Any]:
    if not WINDOW_AHK_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing AHK script: {WINDOW_AHK_SCRIPT}")

    lookup = WINDOW_TITLE_HINTS.get(target.lower(), target)
    result = subprocess.run(
        [str(ahk_exe), str(WINDOW_AHK_SCRIPT), mode, lookup],
        capture_output=True,
        text=True,
        timeout=WINDOW_AHK_TIMEOUT_S,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode == 0:
        return {"ok": True, "action": mode, "target": lookup}
    if result.returncode == 4:
        raise RuntimeError(f"No open window matching '{target}' was found on the PC")
    raise RuntimeError(f"Window action '{mode}' failed with exit code {result.returncode}")


def _launch_application(target: str) -> dict[str, Any]:
    normalized = target.strip().lower()
    executable = APP_LAUNCH_ALIASES.get(normalized, target.strip())
    resolved = shutil.which(executable)

    try:
        if resolved:
            subprocess.Popen([resolved], creationflags=CREATE_NO_WINDOW)
        elif os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(executable)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([executable])
    except OSError as exc:
        raise RuntimeError(f"Could not launch '{target}': {exc}") from exc

    return {"ok": True, "action": "launch_app", "launched": executable}
