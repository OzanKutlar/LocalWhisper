"""Windows PC client for LocalWhisper: receives text over WebSocket and sends it via AHK v1.1."""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import websockets

from ahk_payload import build_send_units, plan_typing_delay_ms
from handoff_client import download_handoff_file, handle_pushed_text, http_base_from_ws
from overlay.settings import OverlaySettings
from overlay.step_aside import requires_step_aside
from pc_clipboard import write_clipboard_text
from pc_tools import dispatch_pc_tool, enable_dpi_awareness

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("localwhisper.pc_client")

CLIENT_DIR = Path(__file__).resolve().parent
AHK_SCRIPT = CLIENT_DIR / "send_keys.ahk"
AHK_TIMEOUT_S = 25  # covers paced typing (~8 s budget plus pauses)
MAX_RECONNECT_ATTEMPTS = 10000
MAX_BACKOFF_S = 15
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_autohotkey_v1(explicit_path: str | None = None) -> Path:
    """Locates the AutoHotkey v1.1 executable on Windows."""
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)
    env_path = os.environ.get("AHK_PATH")
    if env_path:
        candidates.append(env_path)

    # Prefer explicit v1.1 subfolder first (handles dual v1+v2 AutoHotkey installations)
    for base in (r"C:\Program Files\AutoHotkey", r"C:\Program Files (x86)\AutoHotkey"):
        candidates.extend(sorted(glob.glob(os.path.join(base, "v1.1*", "AutoHotkeyU64.exe")), reverse=True))
        candidates.extend(sorted(glob.glob(os.path.join(base, "v1.1*", "AutoHotkey.exe")), reverse=True))
        candidates.append(os.path.join(base, "AutoHotkeyU64.exe"))
        candidates.append(os.path.join(base, "AutoHotkey.exe"))

    for exe_name in ("AutoHotkeyU64.exe", "AutoHotkey.exe"):
        resolved = shutil.which(exe_name)
        if resolved:
            candidates.append(resolved)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path

    raise FileNotFoundError(
        "AutoHotkey v1.1 executable not found. Install AutoHotkey v1.1 or pass --ahk-path."
    )


def execute_ahk_send(ahk_exe: Path, text: str, send_mode: str, human_typing: bool) -> None:
    """Writes escaped send units to a UTF-8 temp file and invokes send_keys.ahk without stealing focus."""
    if not AHK_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing AHK script: {AHK_SCRIPT}")
    if not text:
        raise ValueError("Keystroke payload is empty")

    units = build_send_units(text)
    if not units:
        raise ValueError("Keystroke payload has nothing to type")
    delay_ms = plan_typing_delay_ms(len(units), human_typing)
    # One unit per line when pacing; a single line is sent in one burst.
    payload = "\n".join(units) if delay_ms > 0 else "".join(units)

    fd, temp_path = tempfile.mkstemp(prefix="localwhisper_keys_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)

        result = subprocess.run(
            [str(ahk_exe), str(AHK_SCRIPT), temp_path, send_mode, str(delay_ms)],
            capture_output=True,
            text=True,
            timeout=AHK_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"AutoHotkey exited with code {result.returncode}" + (f": {stderr}" if stderr else "")
            )
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError as exc:
            LOGGER.warning("Could not remove temp payload %s: %s", temp_path, exc)


def build_ws_url(server_base: str, client_name: str) -> str:
    cleaned = server_base.strip().rstrip("/")
    if cleaned.startswith("http://"):
        cleaned = "ws://" + cleaned[len("http://"):]
    elif cleaned.startswith("https://"):
        cleaned = "wss://" + cleaned[len("https://"):]
    elif not cleaned.startswith(("ws://", "wss://")):
        cleaned = "ws://" + cleaned

    if not cleaned.endswith("/ws/client"):
        cleaned = f"{cleaned}/ws/client"
    query = urlencode({"name": client_name})
    return f"{cleaned}?{query}"


async def handle_message(
    websocket: websockets.ClientConnection,
    raw_message: str,
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    server_http_base: str = "",
    on_welcome: Callable[[str, str], None] | None = None,
    step_aside_runner: Callable[[Callable[[], Any]], Any] | None = None,
    on_phone_notification: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        LOGGER.warning("Received invalid JSON from server")
        return

    msg_type = message.get("type")
    if msg_type == "welcome":
        cid = str(message.get("client_id", "") or "")
        cname = str(message.get("name", "") or "")
        LOGGER.info("Registered with server as '%s' [id=%s]", cname, cid)
        if on_welcome is not None:
            on_welcome(cid, cname)
        return

    if msg_type == "file_offer":
        if server_http_base:
            asyncio.create_task(_download_file_offer(server_http_base, message))
        return

    if msg_type == "clipboard_push":
        pushed = str(message.get("text", "") or "")
        if pushed:
            await asyncio.to_thread(_apply_clipboard_push, pushed)
        return

    if msg_type == "phone_notification":
        ev = str(message.get("event", "") or "")
        notif = message.get("notification")
        if ev and isinstance(notif, dict) and on_phone_notification is not None:
            on_phone_notification(ev, notif)
        return

    if msg_type == "tool_call":
        request_id = str(message.get("id", ""))
        tool_name = str(message.get("tool", ""))
        raw_args = message.get("arguments")
        arguments = raw_args if isinstance(raw_args, dict) else {}
        LOGGER.info("Executing agent tool '%s'", tool_name)
        def _invoke_tool() -> dict[str, Any]:
            call = lambda: dispatch_pc_tool(
                tool_name,
                arguments,
                ahk_exe,
                send_mode,
                human_typing,
                execute_ahk_send,
                server_http_base,
            )
            if step_aside_runner is not None and requires_step_aside(tool_name):
                return step_aside_runner(call)
            return call()

        try:
            result = await asyncio.to_thread(_invoke_tool)
            reply = {"type": "tool_result", "id": request_id, "ok": True, "result": result}
        except Exception as exc:
            LOGGER.error("Agent tool '%s' failed: %s", tool_name, exc)
            reply = {"type": "tool_result", "id": request_id, "ok": False, "error": str(exc)}
        await websocket.send(json.dumps(reply))
        return

    if msg_type != "send":
        return

    request_id = str(message.get("id", ""))
    text = str(message.get("text", ""))
    LOGGER.info("Executing keystrokes (%d chars) via AHK (%s)", len(text), send_mode)
    def _invoke_send() -> None:
        call = lambda: execute_ahk_send(ahk_exe, text, send_mode, human_typing)
        if step_aside_runner is not None:
            step_aside_runner(call)
        else:
            call()

    try:
        await asyncio.to_thread(_invoke_send)
        ack = {"type": "ack", "id": request_id, "ok": True}
    except Exception as exc:
        LOGGER.error("AHK send failed: %s", exc)
        ack = {"type": "ack", "id": request_id, "ok": False, "error": str(exc)}

    await websocket.send(json.dumps(ack))


async def _download_file_offer(server_http_base: str, offer: dict[str, object]) -> None:
    try:
        saved = await asyncio.to_thread(download_handoff_file, server_http_base, offer)
        LOGGER.info("Received file from handoff: %s", saved)
    except Exception as exc:  # noqa: BLE001 - background download must not crash client loop
        LOGGER.warning("Failed to download offered file '%s': %s", offer.get("name"), exc)


def _apply_clipboard_push(text: str) -> None:
    try:
        outcome = handle_pushed_text(text, write_clipboard_text, webbrowser.open)
        LOGGER.info("Applied pushed text: %s", outcome.get("action"))
    except Exception as exc:  # noqa: BLE001 - never break WS loop
        LOGGER.warning("Failed to apply pushed clipboard/link: %s", exc)


async def run_client(
    server_url: str,
    client_name: str,
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    on_welcome: Callable[[str, str], None] | None = None,
    step_aside_runner: Callable[[Callable[[], Any]], Any] | None = None,
    on_phone_notification: Callable[[str, dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    ws_url = build_ws_url(server_url, client_name)
    server_http_base = http_base_from_ws(server_url)
    LOGGER.info("Using Python: %s", sys.executable)
    LOGGER.info("Using AutoHotkey: %s", ahk_exe)
    LOGGER.info("Target server: %s", ws_url)

    backoff_s = 2
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        if stop_event is not None and stop_event.is_set():
            return
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=4_194_304,
            ) as websocket:
                backoff_s = 2
                async for raw_message in websocket:
                    if stop_event is not None and stop_event.is_set():
                        return
                    if isinstance(raw_message, str):
                        await handle_message(
                            websocket,
                            raw_message,
                            ahk_exe,
                            send_mode,
                            human_typing,
                            server_http_base,
                            on_welcome=on_welcome,
                            step_aside_runner=step_aside_runner,
                            on_phone_notification=on_phone_notification,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "Connection error (attempt %d/%d): %s. Reconnecting in %ds...",
                attempt,
                MAX_RECONNECT_ATTEMPTS,
                exc,
                backoff_s,
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, MAX_BACKOFF_S)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LocalWhisper Windows AHK v1.1 PC client.")
    parser.add_argument(
        "--server",
        default=os.environ.get("LOCALWHISPER_SERVER", "ws://127.0.0.1:8765"),
        help="Server URL (default: ws://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("LOCALWHISPER_CLIENT_NAME", socket.gethostname()),
        help="Display name shown on Android (default: PC hostname)",
    )
    parser.add_argument(
        "--ahk-path",
        default=None,
        help="Explicit path to AutoHotkeyU64.exe / AutoHotkey.exe (v1.1)",
    )
    parser.add_argument(
        "--send-mode",
        choices=("Input", "Event", "Play"),
        default="Input",
        help="AutoHotkey SendMode (default: Input; use Event if a game ignores SendInput)",
    )
    parser.add_argument(
        "--typing",
        choices=("human", "instant"),
        default="human",
        help="Typing pace (default: human; instant sends everything at once)",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Run headless without the PC Jarvis popup overlay",
    )
    parser.add_argument(
        "--hotkey",
        default=None,
        help="Global hotkey to summon the Jarvis popup (default: Ctrl+Alt+Space)",
    )
    parser.add_argument(
        "--start-typing",
        action="store_true",
        help="Open the PC popup in keyboard mode by default instead of listening",
    )
    parser.add_argument(
        "--dictation-default",
        action="store_true",
        help="Open the PC popup in raw dictation mode instead of Agent mode",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional ASR language hint for PC voice recordings",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Window rects, OCR boxes and AHK clicks must all use physical pixels.
    LOGGER.info("DPI awareness: %s", enable_dpi_awareness())
    try:
        ahk_exe = find_autohotkey_v1(args.ahk_path)
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 1

    human_typing = args.typing == "human"
    if not args.no_overlay:
        try:
            from overlay.app import run_with_overlay  # noqa: PLC0415
        except ImportError as exc:
            LOGGER.warning(
                "PySide6 overlay unavailable (%s); running headless. "
                'Install with: "%s" -m pip install -r pc_client/requirements.txt',
                exc,
                sys.executable,
            )
        else:
            settings = OverlaySettings.load()
            if args.hotkey:
                settings.hotkey = args.hotkey.strip()
            if args.start_typing:
                settings.start_in_keyboard_mode = True
            if args.dictation_default:
                settings.start_in_agent_mode = False
            if args.language is not None:
                settings.language = args.language.strip()
            settings.save()

            def _ws_thread_runner(controller: Any, stop_evt: threading.Event) -> None:
                try:
                    asyncio.run(
                        run_client(
                            args.server,
                            args.name,
                            ahk_exe,
                            args.send_mode,
                            human_typing,
                            on_welcome=controller.set_own_client_id,
                            step_aside_runner=controller.step_aside.run_with_step_aside,
                            on_phone_notification=controller.on_phone_notification_message,
                            stop_event=stop_evt,
                        )
                    )
                except Exception as exc:
                    LOGGER.warning("WebSocket loop stopped: %s", exc)

            return run_with_overlay(
                server_url=args.server,
                server_http_base=http_base_from_ws(args.server),
                client_name=args.name,
                ahk_exe=ahk_exe,
                send_mode=args.send_mode,
                human_typing=human_typing,
                settings=settings,
                execute_ahk_send_fn=execute_ahk_send,
                ws_runner_fn=_ws_thread_runner,
            )

    try:
        asyncio.run(
            run_client(args.server, args.name, ahk_exe, args.send_mode, human_typing)
        )
    except KeyboardInterrupt:
        LOGGER.info("LocalWhisper PC client stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
