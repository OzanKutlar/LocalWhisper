"""PC-side agent tool implementations for LocalWhisper (AHK macros, apps, media, URLs)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import shutil
import subprocess
import tempfile
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
MAX_WINDOWS_RETURNED = 60
MAX_CHILD_CONTROLS = 40
MAX_OCR_LINES = 120
OCR_TIMEOUT_S = 12
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

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
        "foreground_window": foreground_win,
        "total_count": len(matched),
        "windows": matched[:MAX_WINDOWS_RETURNED],
    }


def run_inspect_pc_screen(arguments: dict[str, Any]) -> dict[str, Any]:
    """Inspects the PC screen: active window, visible windows, UI controls, and screen OCR text."""
    window_query = str(arguments.get("window_query", "")).strip()
    include_ocr = bool(arguments.get("include_ocr", True))

    all_windows, _fg_hex = _enumerate_win32_windows()
    visible_windows = [w for w in all_windows if w.get("state") != "minimized"]

    target_window: dict[str, Any] | None = None
    if window_query:
        matches = filter_windows_list(all_windows, query=window_query, include_minimized=True)
        if matches:
            target_window = matches[0]
    if target_window is None:
        target_window = next(
            (w for w in all_windows if w.get("is_foreground")),
            visible_windows[0] if visible_windows else None,
        )

    target_hwnd = int(target_window["_hwnd_int"]) if target_window and "_hwnd_int" in target_window else 0
    ui_controls = _extract_window_child_texts(target_hwnd) if target_hwnd else []

    clean_target = _strip_internal_fields(target_window) if target_window else None
    clean_visible = [_strip_internal_fields(w) for w in visible_windows[:20]]

    ocr_lines: list[str] = []
    ocr_status = "skipped"
    if include_ocr:
        bounds = clean_target.get("bounds") if (clean_target and window_query) else None
        ocr_lines, ocr_status = _capture_and_ocr_pc_screen(bounds)

    return {
        "ok": True,
        "target_window": clean_target,
        "visible_windows": clean_visible,
        "ui_controls": ui_controls,
        "ocr_status": ocr_status,
        "screen_ocr_lines": ocr_lines,
    }


def _strip_internal_fields(win: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in win.items() if not k.startswith("_")}


def _enumerate_win32_windows() -> tuple[list[dict[str, Any]], str | None]:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
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
    if pid <= 0 or os.name != "nt" or not hasattr(ctypes, "windll"):
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
    if parent_hwnd <= 0 or os.name != "nt" or not hasattr(ctypes, "windll"):
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


def _capture_and_ocr_pc_screen(bounds: dict[str, int] | None = None) -> tuple[list[str], str]:
    if os.name != "nt":
        return [], "unsupported_os"

    fd, temp_png = tempfile.mkstemp(prefix="localwhisper_screen_", suffix=".png")
    os.close(fd)

    if bounds and bounds.get("width", 0) > 40 and bounds.get("height", 0) > 40:
        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        w = min(int(bounds.get("width", 1920)), 3840)
        h = min(int(bounds.get("height", 1080)), 2160)
        rect_setup = f"$x={x}; $y={y}; $w={w}; $h={h};"
    else:
        rect_setup = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "$x=$b.X; $y=$b.Y; $w=$b.Width; $h=$b.Height;"
        )

    ps_script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Drawing; "
        f"{rect_setup} "
        "$bmp = New-Object System.Drawing.Bitmap($w, $h); "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size); "
        "$g.Dispose(); "
        f"$bmp.Save('{temp_png}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$bmp.Dispose(); "
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
        "$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]; "
        "$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]; "
        "$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]; "
        "$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { "
        "$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and "
        "$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]; "
        "function Await($op, $t) { $m = $asTask.MakeGenericMethod($t); "
        "$net = $m.Invoke($null, @($op)); $net.Wait(8000) | Out-Null; return $net.Result }; "
        f"$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{temp_png}')) ([Windows.Storage.StorageFile]); "
        "$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream]); "
        "$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder]); "
        "$sbmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap]); "
        "$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages(); "
        "if ($null -eq $eng) { $stream.Dispose(); exit 0 }; "
        "$res = Await ($eng.RecognizeAsync($sbmp)) ([Windows.Media.Ocr.OcrResult]); "
        "$stream.Dispose(); "
        "foreach ($line in $res.Lines) { Write-Output $line.Text }"
    )

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=OCR_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return [], f"ocr_error: {result.stderr.strip()[:160]}"
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ][:MAX_OCR_LINES]
        return lines, "ok"
    except Exception as exc:
        return [], f"ocr_failed: {exc}"
    finally:
        try:
            if os.path.exists(temp_png):
                os.remove(temp_png)
        except OSError:
            pass
