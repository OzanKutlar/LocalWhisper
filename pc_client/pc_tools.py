"""PC-side agent tool implementations for LocalWhisper (AHK macros, apps, media, URLs, screen clicks)."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from screen_elements import (
    MAX_ELEMENTS,
    ScreenSnapshot,
    SnapshotCache,
    build_elements,
    clamp_offset,
    click_point,
    parse_ocr_lines,
    resolve_text_target,
    staleness_reason,
)

CLIENT_DIR = Path(__file__).resolve().parent
WINDOW_AHK_SCRIPT = CLIENT_DIR / "window_action.ahk"
MOUSE_AHK_SCRIPT = CLIENT_DIR / "mouse_action.ahk"
SCREEN_SCRIPT = CLIENT_DIR / "screen_capture.ps1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MAX_MACRO_STEPS = 25
MAX_STEP_WAIT_MS = 5000
MAX_VOLUME_STEPS = 20
WINDOW_AHK_TIMEOUT_S = 6
MOUSE_AHK_TIMEOUT_S = 6
MAX_WINDOWS_RETURNED = 60
MAX_VISIBLE_WINDOWS_LISTED = 12
MAX_CHILD_CONTROLS = 40
SCREEN_SCRIPT_TIMEOUT_S = 20
MIN_CAPTURE_PX = 40
MAX_CAPTURE_W = 7680
MAX_CAPTURE_H = 4320
OCR_UPSCALE_BELOW_WIDTH = 1200
PREVIEW_MAX_WIDTH = 720
PREVIEW_FALLBACK_W = 720
PREVIEW_FALLBACK_H = 480
PREVIEW_MAX_BYTES = 400_000
SCREENSHOT_DEFAULT_MAX_WIDTH = 1280
SCREENSHOT_MIN_WIDTH = 320
SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_BYTES = 650_000
SCREENSHOT_RETRY_WIDTH = 960
SCREENSHOT_PRIMARY_QUALITY = 80
SCREENSHOT_FALLBACK_QUALITY = 62
MAX_IMAGE_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif', '.tiff'})
AFTER_ACTION_DELAY_S = 0.4
AFTER_ACTION_ELEMENT_LIMIT = 60
FOCUS_SETTLE_S = 0.15
TYPE_AFTER_CLICK_DELAY_S = 0.15
MAX_SCROLL_NOTCHES = 15
DEFAULT_SCROLL_NOTCHES = 3
MAX_TYPE_CHARS = 4000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
GA_ROOT = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4
MOUSE_BUTTONS = {"left": "Left", "right": "Right"}

# Newest OCR snapshots, shared by the inspect, click, type and scroll tools on this PC.
SNAPSHOTS = SnapshotCache()

IGNORED_WINDOW_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "Windows.UI.Core.CoreWindow",
    }
)

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


def enable_dpi_awareness() -> str:
    """Makes this process per-monitor DPI aware so window rects, OCR boxes and clicks share pixels."""
    if not _is_windows():
        return "unsupported_os"
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_V2)):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per_monitor"
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except (AttributeError, OSError):
        return "unaware"


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
    if tool_name == "list_pc_windows":
        return run_list_pc_windows(arguments)
    if tool_name == "inspect_pc_screen":
        return run_inspect_pc_screen(arguments)
    if tool_name == "capture_pc_screenshot":
        return run_capture_pc_screenshot(arguments)
    if tool_name == "read_pc_image":
        return run_read_pc_image(arguments)
    if tool_name == "prepare_click":
        return run_prepare_click(arguments)
    if tool_name == "click_screen_element":
        return run_click_screen_element(arguments, ahk_exe)
    if tool_name == "type_into_element":
        return run_type_into_element(arguments, ahk_exe, send_mode, human_typing, ahk_sender)
    if tool_name == "scroll_pc":
        return run_scroll_pc(arguments, ahk_exe)
    raise ValueError(f"Unsupported PC tool: {tool_name}")


# ---------------------------------------------------------------- Keystrokes, media, URLs, apps

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
        if _is_windows():
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


# ---------------------------------------------------------------- Window listing

def filter_windows_list(
    windows: list[dict[str, Any]],
    query: str = "",
    include_minimized: bool = True,
) -> list[dict[str, Any]]:
    """Filters a list of window dictionaries by minimized state and search tokens."""
    tokens = [tok for tok in query.strip().lower().split() if tok]
    filtered: list[dict[str, Any]] = []
    for win in windows:
        if not include_minimized and win.get("state") == "minimized":
            continue
        if tokens:
            haystack = " ".join(
                [
                    str(win.get("title", "")),
                    str(win.get("process_name", "")),
                    str(win.get("class_name", "")),
                    str(win.get("pid", "")),
                    str(win.get("hwnd", "")),
                ]
            ).lower()
            if not all(tok in haystack for tok in tokens):
                continue
        filtered.append(win)
    return filtered


def run_list_pc_windows(arguments: dict[str, Any]) -> dict[str, Any]:
    """Enumerates active/open Windows on the PC like a `ps` tool and returns structured JSON."""
    query = str(arguments.get("query", "")).strip()
    include_minimized = bool(arguments.get("include_minimized", True))
    all_windows, fg_hwnd_hex = _enumerate_win32_windows()
    matched = filter_windows_list(all_windows, query=query, include_minimized=include_minimized)
    foreground_win = next((w for w in all_windows if w.get("is_foreground")), None)
    return {
        "ok": True,
        "query": query,
        "foreground_hwnd": fg_hwnd_hex,
        "foreground_window": _strip_internal_fields(foreground_win) if foreground_win else None,
        "total_count": len(matched),
        "windows": [_strip_internal_fields(w) for w in matched[:MAX_WINDOWS_RETURNED]],
    }


# ---------------------------------------------------------------- Screen inspection (OCR snapshots)

def run_inspect_pc_screen(arguments: dict[str, Any]) -> dict[str, Any]:
    """Inspects the PC screen and returns an OCR snapshot whose elements can be clicked by id."""
    window_query = str(arguments.get("window_query", "")).strip()
    include_ocr = bool(arguments.get("include_ocr", True))

    all_windows, _fg_hex = _enumerate_win32_windows()
    visible_windows = [w for w in all_windows if w.get("state") != "minimized"]
    target, query_matched = _pick_inspect_target(all_windows, visible_windows, window_query)
    target_hwnd = int(target["_hwnd_int"]) if target and "_hwnd_int" in target else 0

    result: dict[str, Any] = {
        "ok": True,
        "target_window": _strip_internal_fields(target) if target else None,
        "window_query_matched": query_matched,
        "target_is_foreground": bool(target and target.get("is_foreground")),
        "visible_windows": [_brief_window(w) for w in visible_windows[:MAX_VISIBLE_WINDOWS_LISTED]],
        "ui_controls": _extract_window_child_texts(target_hwnd) if target_hwnd else [],
    }
    if not include_ocr:
        result["ocr_status"] = "skipped"
        return result
    if target and target.get("state") == "minimized":
        result["ocr_status"] = "target_minimized: focus the window with manage_pc_app first"
        return result

    snapshot, status = _take_snapshot(target, full_screen=not window_query)
    result["ocr_status"] = status
    if snapshot is not None:
        result.update(_snapshot_payload(snapshot, MAX_ELEMENTS))
    return result


def _pick_inspect_target(
    all_windows: list[dict[str, Any]],
    visible_windows: list[dict[str, Any]],
    window_query: str,
) -> tuple[dict[str, Any] | None, bool]:
    if window_query:
        matches = filter_windows_list(all_windows, query=window_query, include_minimized=True)
        if matches:
            return matches[0], True
    foreground = next(
        (w for w in all_windows if w.get("is_foreground")),
        visible_windows[0] if visible_windows else None,
    )
    return foreground, False


def _brief_window(win: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": win.get("title", ""),
        "process_name": win.get("process_name", ""),
        "state": win.get("state", ""),
        "is_foreground": bool(win.get("is_foreground")),
    }


def _snapshot_payload(snapshot: ScreenSnapshot, limit: int) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "element_count": len(snapshot.elements),
        "elements": snapshot.compact(limit),
    }


def _take_snapshot(
    target: dict[str, Any] | None,
    full_screen: bool,
) -> tuple[ScreenSnapshot | None, str]:
    """OCRs the target window (or the primary screen) and stores the result in SNAPSHOTS."""
    hwnd = int(target["_hwnd_int"]) if target and "_hwnd_int" in target else 0
    title = str(target.get("title", "")) if target else ""
    bounds = None if (full_screen or target is None) else target.get("bounds")
    payload, status = _run_screen_script("ocr", _ocr_params(bounds))
    if payload is None:
        return None, status
    elements = build_elements(parse_ocr_lines(payload.get("lines")))
    label = title or "(untitled)"
    if bounds is None:
        label = f"Primary screen (foreground: {label})"
    snapshot = SNAPSHOTS.add(
        hwnd=hwnd,
        window_title=label,
        window_rect=_get_window_rect(hwnd) if hwnd else None,
        elements=elements,
    )
    return snapshot, "ok"


def _ocr_params(bounds: dict[str, int] | None) -> dict[str, int]:
    if not bounds or bounds.get("width", 0) <= MIN_CAPTURE_PX or bounds.get("height", 0) <= MIN_CAPTURE_PX:
        return {"X": 0, "Y": 0, "W": 0, "H": 0, "Scale": 1}
    width = min(int(bounds["width"]), MAX_CAPTURE_W)
    height = min(int(bounds["height"]), MAX_CAPTURE_H)
    scale = 2 if width < OCR_UPSCALE_BELOW_WIDTH else 1
    return {"X": int(bounds["x"]), "Y": int(bounds["y"]), "W": width, "H": height, "Scale": scale}


# ---------------------------------------------------------------- Multimodal images (screenshots & files)

def _clamp_image_max_width(raw_value: Any) -> int:
    parsed = _optional_int(raw_value)
    if parsed is None:
        return SCREENSHOT_DEFAULT_MAX_WIDTH
    return max(SCREENSHOT_MIN_WIDTH, min(parsed, SCREENSHOT_MAX_WIDTH))


def _screenshot_region(bounds: dict[str, int] | None) -> dict[str, int]:
    if not bounds or bounds.get("width", 0) <= MIN_CAPTURE_PX or bounds.get("height", 0) <= MIN_CAPTURE_PX:
        return {"X": 0, "Y": 0, "W": 0, "H": 0}
    return {
        "X": int(bounds["x"]),
        "Y": int(bounds["y"]),
        "W": min(int(bounds["width"]), MAX_CAPTURE_W),
        "H": min(int(bounds["height"]), MAX_CAPTURE_H),
    }


def _capture_encoded_jpeg(
    mode: str,
    base_params: dict[str, Any],
    max_width: int,
) -> tuple[str, int, int, str]:
    """Runs screen_capture.ps1 to produce a bounded JPEG and returns (b64, width, height, status)."""
    attempts = (
        (max_width, SCREENSHOT_PRIMARY_QUALITY),
        (min(max_width, SCREENSHOT_RETRY_WIDTH), SCREENSHOT_FALLBACK_QUALITY),
    )
    last_status = "image_encoding_failed"
    for attempt_width, quality in attempts:
        fd, path = tempfile.mkstemp(prefix="localwhisper_img_", suffix=".jpg")
        os.close(fd)
        try:
            params = {
                **base_params,
                "MaxWidth": attempt_width,
                "Quality": quality,
                "OutPath": path,
            }
            payload, status = _run_screen_script(mode, params)
            if payload is None:
                return "", 0, 0, status
            data = Path(path).read_bytes()
            if not data:
                last_status = "image_empty"
                continue
            if len(data) <= SCREENSHOT_MAX_BYTES:
                width = int(payload.get("width", 0) or 0)
                height = int(payload.get("height", 0) or 0)
                return base64.b64encode(data).decode("ascii"), width, height, "ok"
            last_status = "image_too_large"
        except OSError as exc:
            return "", 0, 0, f"image_io_failed: {exc}"
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    return "", 0, 0, last_status


def run_capture_pc_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    """Captures a clean JPEG screenshot of the primary PC screen or a specific window."""
    window_query = str(arguments.get("window_query", "") or "").strip()
    max_width = _clamp_image_max_width(arguments.get("max_width"))

    all_windows, _fg_hex = _enumerate_win32_windows()
    visible_windows = [w for w in all_windows if w.get("state") != "minimized"]
    target, query_matched = _pick_inspect_target(all_windows, visible_windows, window_query)

    if window_query and query_matched and target and target.get("state") == "minimized":
        return {
            "ok": False,
            "error": "target_minimized: focus the window with manage_pc_app first",
        }

    use_window_bounds = bool(window_query and query_matched and target is not None)
    bounds = target.get("bounds") if use_window_bounds and target is not None else None
    b64, width, height, status = _capture_encoded_jpeg(
        "screenshot",
        _screenshot_region(bounds),
        max_width,
    )
    if status != "ok":
        return {"ok": False, "error": status}

    title = str(target.get("title", "")) if target else ""
    label = title or "(untitled)"
    if not use_window_bounds:
        label = f"Primary screen (foreground: {label})"
    return {
        "ok": True,
        "image_jpeg_b64": b64,
        "mime_type": "image/jpeg",
        "width": width,
        "height": height,
        "window_title": label,
        "window_query_matched": query_matched,
    }


def validate_image_file_path(raw_path: str) -> Path:
    """Validates that raw_path points to an existing, supported, bounded image file."""
    cleaned = str(raw_path or "").strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("Image file path must not be empty")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    path = Path(expanded)
    if not path.is_file():
        raise ValueError(f"Image file not found: {cleaned}")
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise ValueError(f"Unsupported image type '{suffix or '(none)'}'. Supported: {allowed}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Could not read image metadata: {exc}") from exc
    if size <= 0:
        raise ValueError("Image file is empty")
    if size > MAX_IMAGE_FILE_BYTES:
        raise ValueError(f"Image file exceeds {MAX_IMAGE_FILE_BYTES // (1024 * 1024)} MB limit")
    return path.resolve()


def run_read_pc_image(arguments: dict[str, Any]) -> dict[str, Any]:
    """Reads an approved local image file from the PC and returns a resized JPEG payload."""
    raw_path = str(arguments.get("path", "") or "")
    resolved = validate_image_file_path(raw_path)
    max_width = _clamp_image_max_width(arguments.get("max_width"))
    b64, width, height, status = _capture_encoded_jpeg(
        "load_image",
        {"InPath": str(resolved)},
        max_width,
    )
    if status != "ok":
        return {"ok": False, "error": status}
    return {
        "ok": True,
        "image_jpeg_b64": b64,
        "mime_type": "image/jpeg",
        "width": width,
        "height": height,
        "file_name": resolved.name,
        "file_path": str(resolved),
    }


def _run_screen_script(mode: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Runs screen_capture.ps1 and returns (payload, status)."""
    if not _is_windows():
        return None, "unsupported_os"
    if not SCREEN_SCRIPT.is_file():
        return None, f"missing_script: {SCREEN_SCRIPT.name}"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCREEN_SCRIPT),
        f"-Mode:{mode}",
    ]
    command.extend(f"-{name}:{value}" for name, value in params.items())
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCREEN_SCRIPT_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"script_failed: {exc}"
    payload = _parse_last_json_line(result.stdout)
    if payload is None:
        detail = (result.stderr or result.stdout).strip()[:200]
        return None, f"script_error (exit {result.returncode}): {detail}"
    if not payload.get("ok"):
        return None, f"script_error: {payload.get('error', 'unknown')}"
    return payload, "ok"


def _parse_last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


# ---------------------------------------------------------------- Click targets

def resolve_click_target(arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolves element_id, target_text or x/y into a screen point. Never clicks."""
    element_id = str(arguments.get("element_id", "") or "").strip()
    target_text = str(arguments.get("target_text", "") or "").strip()
    if element_id:
        return _resolve_by_element(arguments, element_id)
    if target_text:
        return _resolve_by_text(arguments, target_text)
    if arguments.get("x") is not None and arguments.get("y") is not None:
        return _resolve_by_point(arguments)
    return _not_done("missing_target", "Provide element_id (preferred), target_text, or x and y.")


def _not_done(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "clicked": False, "reason": reason, "message": message, **extra}


def _resolve_by_element(arguments: dict[str, Any], element_id: str) -> dict[str, Any]:
    snapshot_id = str(arguments.get("snapshot_id", "") or "").strip() or None
    snapshot = SNAPSHOTS.get(snapshot_id)
    if snapshot is None:
        return _not_done("no_snapshot", "No matching screen snapshot. Call inspect_pc_screen first.")
    element = snapshot.find(element_id)
    if element is None:
        return _not_done("unknown_element", f"{element_id} is not in snapshot {snapshot.snapshot_id}.")
    stale = staleness_reason(
        snapshot,
        _get_window_rect(snapshot.hwnd),
        time.monotonic(),
        SNAPSHOTS.ttl_s,
    )
    if stale:
        return _not_done("stale_snapshot", stale)
    return _resolved_element(snapshot, element, arguments)


def _resolved_element(snapshot: ScreenSnapshot, element: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    x, y = click_point(element, arguments.get("offset_x", 0), arguments.get("offset_y", 0))
    return {
        "ok": True,
        "mode": "element",
        "snapshot_id": snapshot.snapshot_id,
        "element_id": element.element_id,
        "display_text": element.text,
        "window_title": snapshot.window_title,
        "hwnd": snapshot.hwnd,
        "x": x,
        "y": y,
        "box": element.to_dict(),
    }


def _resolve_by_text(arguments: dict[str, Any], target_text: str) -> dict[str, Any]:
    target = _foreground_window_info()
    snapshot, status = _take_snapshot(target, full_screen=target is None)
    if snapshot is None:
        return _not_done("ocr_failed", status)
    occurrence = _optional_int(arguments.get("occurrence"))
    element, candidates, reason = resolve_text_target(snapshot.elements, target_text, occurrence)
    if element is None:
        return _not_done(
            reason,
            _text_failure_message(reason, target_text),
            snapshot_id=snapshot.snapshot_id,
            candidates=[c.compact() for c in candidates],
        )
    return _resolved_element(snapshot, element, arguments)


def _text_failure_message(reason: str, text: str) -> str:
    if reason == "ambiguous":
        return f"'{text}' appears several times. Retry with an element_id from the candidates or occurrence (1 = topmost)."
    if reason == "bad_occurrence":
        return f"occurrence is out of range for '{text}'. Pick one of the candidates."
    return f"Could not find '{text}' on screen. Closest texts are in candidates; focus the right window or inspect again."


def _resolve_by_point(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_x = _optional_int(arguments.get("x"))
    raw_y = _optional_int(arguments.get("y"))
    if raw_x is None or raw_y is None:
        return _not_done("bad_point", "x and y must be integers.")
    x = raw_x + clamp_offset(arguments.get("offset_x", 0))
    y = raw_y + clamp_offset(arguments.get("offset_y", 0))
    hwnd = _root_window_at(x, y)
    return {
        "ok": True,
        "mode": "point",
        "snapshot_id": "",
        "element_id": "",
        "display_text": "",
        "window_title": _get_window_title(hwnd),
        "hwnd": hwnd,
        "x": x,
        "y": y,
        "box": None,
    }


def run_prepare_click(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read-only: resolves the click target and captures a preview image for the phone approval."""
    target = resolve_click_target(arguments)
    if not target.get("ok"):
        return target
    preview_b64, preview_status = _capture_click_preview(target)
    public = {k: v for k, v in target.items() if k not in ("ok", "hwnd")}
    return {
        "ok": True,
        "resolved": public,
        "preview_jpeg_b64": preview_b64,
        "preview_status": preview_status,
    }


def _capture_click_preview(target: dict[str, Any]) -> tuple[str, str]:
    params: dict[str, Any] = {
        **_preview_region(target),
        **_preview_box(target),
        "PointX": int(target["x"]),
        "PointY": int(target["y"]),
        "MaxWidth": PREVIEW_MAX_WIDTH,
    }
    fd, path = tempfile.mkstemp(prefix="localwhisper_preview_", suffix=".jpg")
    os.close(fd)
    try:
        params["OutPath"] = path
        payload, status = _run_screen_script("preview", params)
        if payload is None:
            return "", status
        data = Path(path).read_bytes()
        if not data or len(data) > PREVIEW_MAX_BYTES:
            return "", "preview_empty_or_too_large"
        return base64.b64encode(data).decode("ascii"), "ok"
    except OSError as exc:
        return "", f"preview_failed: {exc}"
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _preview_region(target: dict[str, Any]) -> dict[str, int]:
    x, y = int(target["x"]), int(target["y"])
    rect = _get_window_rect(int(target.get("hwnd") or 0))
    if rect and rect["width"] > MIN_CAPTURE_PX and rect["height"] > MIN_CAPTURE_PX and _rect_contains(rect, x, y):
        return {
            "X": rect["x"],
            "Y": rect["y"],
            "W": min(rect["width"], MAX_CAPTURE_W),
            "H": min(rect["height"], MAX_CAPTURE_H),
        }
    return {
        "X": x - PREVIEW_FALLBACK_W // 2,
        "Y": y - PREVIEW_FALLBACK_H // 2,
        "W": PREVIEW_FALLBACK_W,
        "H": PREVIEW_FALLBACK_H,
    }


def _preview_box(target: dict[str, Any]) -> dict[str, int]:
    box = target.get("box")
    if isinstance(box, dict):
        return {"BoxX": int(box["x"]), "BoxY": int(box["y"]), "BoxW": int(box["w"]), "BoxH": int(box["h"])}
    return {"BoxX": 0, "BoxY": 0, "BoxW": 0, "BoxH": 0}


def _rect_contains(rect: dict[str, int], x: int, y: int) -> bool:
    return rect["x"] <= x < rect["x"] + rect["width"] and rect["y"] <= y < rect["y"] + rect["height"]


# ---------------------------------------------------------------- Click, type, scroll

def run_click_screen_element(arguments: dict[str, Any], ahk_exe: Path) -> dict[str, Any]:
    """Clicks an approved target and returns a fresh 'after' snapshot."""
    target = resolve_click_target(arguments)
    if not target.get("ok"):
        return target
    _focus_target_window(ahk_exe, int(target.get("hwnd") or 0))
    clicks = "2" if bool(arguments.get("double", False)) else "1"
    _run_mouse_ahk(ahk_exe, ["click", str(target["x"]), str(target["y"]), _button_name(arguments), clicks])
    return _after_action_result(target, "clicked")


def run_type_into_element(
    arguments: dict[str, Any],
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    ahk_sender: Callable[[Path, str, str, bool], None],
) -> dict[str, Any]:
    """Clicks an approved target to focus it, then types text (AHK brace tokens allowed)."""
    text = str(arguments.get("text", ""))
    if not text:
        raise ValueError("'text' to type must not be empty")
    if len(text) > MAX_TYPE_CHARS:
        raise ValueError(f"'text' exceeds {MAX_TYPE_CHARS} characters")
    target = resolve_click_target(arguments)
    if not target.get("ok"):
        return target
    _focus_target_window(ahk_exe, int(target.get("hwnd") or 0))
    _run_mouse_ahk(ahk_exe, ["click", str(target["x"]), str(target["y"]), "Left", "1"])
    time.sleep(TYPE_AFTER_CLICK_DELAY_S)
    ahk_sender(ahk_exe, text, send_mode, human_typing)
    return _after_action_result(target, "typed")


def run_scroll_pc(arguments: dict[str, Any], ahk_exe: Path) -> dict[str, Any]:
    """Scrolls over an element, or over the center of the foreground window."""
    direction = str(arguments.get("direction", "down")).strip().lower()
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    requested = _optional_int(arguments.get("amount"))
    amount = max(1, min(requested if requested is not None else DEFAULT_SCROLL_NOTCHES, MAX_SCROLL_NOTCHES))
    if str(arguments.get("element_id", "") or "").strip():
        target = resolve_click_target(arguments)
        if not target.get("ok"):
            return target
    else:
        target = _foreground_center_target()
    _focus_target_window(ahk_exe, int(target.get("hwnd") or 0))
    _run_mouse_ahk(ahk_exe, ["scroll", str(target["x"]), str(target["y"]), direction, str(amount)])
    return _after_action_result(target, "scrolled")


def _foreground_center_target() -> dict[str, Any]:
    info = _foreground_window_info()
    if info is None:
        raise RuntimeError("No foreground window to scroll")
    bounds = info["bounds"]
    return {
        "ok": True,
        "mode": "point",
        "hwnd": info["_hwnd_int"],
        "element_id": "",
        "display_text": "",
        "window_title": info["title"],
        "x": bounds["x"] + bounds["width"] // 2,
        "y": bounds["y"] + bounds["height"] // 2,
    }


def _after_action_result(target: dict[str, Any], action: str) -> dict[str, Any]:
    """Invalidates old snapshots of the window and returns a fresh snapshot of the new screen."""
    hwnd = int(target.get("hwnd") or 0)
    if hwnd:
        SNAPSHOTS.invalidate_window(hwnd)
    time.sleep(AFTER_ACTION_DELAY_S)
    after_target = _foreground_window_info()
    snapshot, status = _take_snapshot(after_target, full_screen=after_target is None)
    result: dict[str, Any] = {
        "ok": True,
        action: True,
        "element_id": target.get("element_id", ""),
        "display_text": target.get("display_text", ""),
        "point": {"x": target["x"], "y": target["y"]},
    }
    if snapshot is None:
        result["after"] = {"ocr_status": status}
    else:
        result["after"] = {"ocr_status": "ok", **_snapshot_payload(snapshot, AFTER_ACTION_ELEMENT_LIMIT)}
    return result


def _focus_target_window(ahk_exe: Path, hwnd: int) -> None:
    if hwnd <= 0 or hwnd == _foreground_hwnd():
        return
    _run_window_ahk(ahk_exe, "focus", f"ahk_id {hwnd}")
    time.sleep(FOCUS_SETTLE_S)


def _run_mouse_ahk(ahk_exe: Path, args: list[str]) -> None:
    if not MOUSE_AHK_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing AHK script: {MOUSE_AHK_SCRIPT}")
    result = subprocess.run(
        [str(ahk_exe), str(MOUSE_AHK_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=MOUSE_AHK_TIMEOUT_S,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Mouse action '{args[0]}' failed with exit code {result.returncode}")


def _button_name(arguments: dict[str, Any]) -> str:
    return MOUSE_BUTTONS.get(str(arguments.get("button", "left")).strip().lower(), "Left")


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Win32 helpers

def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def _foreground_hwnd() -> int:
    if not _is_windows():
        return 0
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _foreground_window_info() -> dict[str, Any] | None:
    hwnd = _foreground_hwnd()
    if not hwnd:
        return None
    rect = _get_window_rect(hwnd)
    if rect is None:
        return None
    return {"_hwnd_int": hwnd, "title": _get_window_title(hwnd), "bounds": rect, "is_foreground": True}


def _get_window_rect(hwnd: int) -> dict[str, int] | None:
    if hwnd <= 0 or not _is_windows():
        return None
    user32 = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    if not user32.IsWindow(handle):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        return None
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def _get_window_title(hwnd: int) -> str:
    if hwnd <= 0 or not _is_windows():
        return ""
    user32 = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    length = int(user32.GetWindowTextLengthW(handle) or 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buf, length + 1)
    return buf.value.strip()


def _root_window_at(x: int, y: int) -> int:
    if not _is_windows():
        return 0
    user32 = ctypes.windll.user32
    hwnd = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
    if not hwnd:
        return 0
    root = int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or 0)
    return root or hwnd


def _strip_internal_fields(win: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in win.items() if not k.startswith("_")}


def _enumerate_win32_windows() -> tuple[list[dict[str, Any]], str | None]:
    if not _is_windows():
        return [], None

    user32 = ctypes.windll.user32
    fg_hwnd = int(user32.GetForegroundWindow() or 0)
    fg_hex = hex(fg_hwnd) if fg_hwnd else None
    collected: list[dict[str, Any]] = []

    wnd_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd) or 0)
        if length <= 0:
            return True

        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()
        if not title:
            return True

        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value.strip()
        if class_name in IGNORED_WINDOW_CLASSES:
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = _resolve_process_name(int(pid.value))

        is_minimized = bool(user32.IsIconic(hwnd))
        is_maximized = bool(user32.IsZoomed(hwnd))
        state = "minimized" if is_minimized else ("maximized" if is_maximized else "normal")

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(0, int(rect.right - rect.left))
        height = max(0, int(rect.bottom - rect.top))
        if width == 0 and height == 0 and not is_minimized:
            return True

        is_fg = int(hwnd) == fg_hwnd
        collected.append(
            {
                "hwnd": hex(int(hwnd)),
                "_hwnd_int": int(hwnd),
                "pid": int(pid.value),
                "process_name": process_name,
                "title": title,
                "class_name": class_name,
                "is_foreground": is_fg,
                "state": state,
                "bounds": {
                    "x": int(rect.left),
                    "y": int(rect.top),
                    "width": width,
                    "height": height,
                },
            }
        )
        return True

    user32.EnumWindows(wnd_enum_proc(_callback), 0)
    collected.sort(
        key=lambda w: (
            0 if w["is_foreground"] else (2 if w["state"] == "minimized" else 1),
            str(w["process_name"]).lower(),
        )
    )
    return collected, fg_hex


def _resolve_process_name(pid: int) -> str:
    if pid <= 0 or not _is_windows():
        return "unknown"
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "unknown"
    try:
        buf_len = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
            full_path = buf.value
            return os.path.basename(full_path) or full_path
        return "unknown"
    finally:
        kernel32.CloseHandle(handle)


def _extract_window_child_texts(parent_hwnd: int) -> list[dict[str, str]]:
    if parent_hwnd <= 0 or not _is_windows():
        return []

    user32 = ctypes.windll.user32
    controls: list[dict[str, str]] = []
    seen: set[str] = set()
    wnd_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _child_cb(hwnd: int, _lparam: int) -> bool:
        if len(controls) >= MAX_CHILD_CONTROLS:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True

        text = _read_hwnd_text(user32, hwnd)
        if not text or len(text) > 300 or text in seen:
            return True

        class_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, class_buf, 128)
        seen.add(text)
        controls.append({"role": class_buf.value, "text": text})
        return True

    user32.EnumChildWindows(parent_hwnd, wnd_enum_proc(_child_cb), 0)
    return controls


def _read_hwnd_text(user32: Any, hwnd: int) -> str:
    length = int(user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0) or 0)
    if length <= 0 or length > 2048:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.byref(buf))
    return buf.value.strip()
