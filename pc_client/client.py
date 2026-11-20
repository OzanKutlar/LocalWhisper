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
from pathlib import Path
from urllib.parse import urlencode

import websockets

from ahk_payload import build_send_units, plan_typing_delay_ms
from pc_tools import dispatch_pc_tool

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
) -> None:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        LOGGER.warning("Received invalid JSON from server")
        return

    msg_type = message.get("type")
    if msg_type == "welcome":
        LOGGER.info(
            "Registered with server as '%s' [id=%s]",
            message.get("name"),
            message.get("client_id"),
        )
        return

    if msg_type == "tool_call":
        request_id = str(message.get("id", ""))
        tool_name = str(message.get("tool", ""))
        raw_args = message.get("arguments")
        arguments = raw_args if isinstance(raw_args, dict) else {}
        LOGGER.info("Executing agent tool '%s'", tool_name)
        try:
            result = await asyncio.to_thread(
                dispatch_pc_tool,
                tool_name,
                arguments,
                ahk_exe,
                send_mode,
                human_typing,
                execute_ahk_send,
            )
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
    try:
        await asyncio.to_thread(execute_ahk_send, ahk_exe, text, send_mode, human_typing)
        ack = {"type": "ack", "id": request_id, "ok": True}
    except Exception as exc:
        LOGGER.error("AHK send failed: %s", exc)
        ack = {"type": "ack", "id": request_id, "ok": False, "error": str(exc)}

    await websocket.send(json.dumps(ack))


async def run_client(
    server_url: str,
    client_name: str,
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
) -> None:
    ws_url = build_ws_url(server_url, client_name)
    LOGGER.info("Using AutoHotkey: %s", ahk_exe)
    LOGGER.info("Target server: %s", ws_url)

    backoff_s = 2
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=1_048_576,
            ) as websocket:
                backoff_s = 2
                async for raw_message in websocket:
                    if isinstance(raw_message, str):
                        await handle_message(websocket, raw_message, ahk_exe, send_mode, human_typing)
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ahk_exe = find_autohotkey_v1(args.ahk_path)
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        asyncio.run(
            run_client(args.server, args.name, ahk_exe, args.send_mode, args.typing == "human")
        )
    except KeyboardInterrupt:
        LOGGER.info("LocalWhisper PC client stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
