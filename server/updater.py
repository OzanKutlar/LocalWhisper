"""Detached updater worker for LocalWhisper server: waits for shutdown, runs git pull, relaunches."""

from __future__ import annotations

import argparse
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("localwhisper.updater")

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent
MAIN_SCRIPT = SERVER_DIR / "main.py"
LAST_UPDATE_FILE = SERVER_DIR / ".last_update.json"

WAIT_EXIT_TIMEOUT_S = 15.0
GIT_TIMEOUT_S = 60
STATUS_HOLD_AFTER_PULL_S = 1.1
PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_git_commit(repo_dir: Path) -> str:
    """Returns the current short git commit hash of repo_dir, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def summarize_git_output(raw_output: str) -> str:
    """Condenses git pull stdout/stderr into a concise human-readable line."""
    lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    if not lines:
        return "git pull completed"
    for line in lines:
        lower = line.lower()
        if "already up to date" in lower or "already up-to-date" in lower:
            return "Already up to date"
        if "updating " in lower or "fast-forward" in lower:
            return " | ".join(lines[:3])[:220]
    return " | ".join(lines[:3])[:220]


def run_git_pull(repo_dir: Path) -> tuple[bool, str, str]:
    """Executes `git pull` in repo_dir and returns (ok, full_output, summary)."""
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
        combined = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        ).strip()
        ok = result.returncode == 0
        summary = summarize_git_output(combined or ("Success" if ok else "git pull failed"))
        return ok, combined, summary
    except Exception as exc:
        msg = f"git pull error: {exc}"
        return False, msg, msg


def write_update_state(state: dict[str, Any], path: Path = LAST_UPDATE_FILE) -> None:
    """Persists the current or final update state to disk as JSON."""
    try:
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        LOGGER.warning("Failed to write update state file %s: %s", path, exc)


def read_update_state(path: Path = LAST_UPDATE_FILE) -> dict[str, Any]:
    """Reads the most recent update state from disk, or returns a default dictionary."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt" and hasattr(ctypes, "windll"):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_port_free(host: str, port: int) -> bool:
    bind_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((bind_host, port)) != 0


def wait_for_server_close(parent_pid: int, host: str, port: int) -> None:
    deadline = time.monotonic() + WAIT_EXIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not is_pid_running(parent_pid) and is_port_free(host, port):
            return
        time.sleep(0.15)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_temporary_status_server(
    host: str,
    port: int,
    shared_state: dict[str, Any],
    state_lock: threading.Lock,
) -> ThreadingHTTPServer | None:
    """Binds the server port temporarily while main.py is closed so Android gets live status."""

    class _StatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/update/status") or self.path.startswith("/api/health"):
                with state_lock:
                    payload = json.dumps(shared_state).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(503)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    try:
        server = _ReusableThreadingHTTPServer((host, port), _StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
    except OSError as exc:
        LOGGER.warning("Could not bind temporary status server on %s:%d: %s", host, port, exc)
        return None


def relaunch_main_server(relaunch_args: list[str]) -> None:
    cmd = [sys.executable, str(MAIN_SCRIPT), *relaunch_args]
    LOGGER.info("Relaunching LocalWhisper server: %s", " ".join(cmd))
    if os.name == "nt":
        subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            start_new_session=True,
        )


def execute_update_workflow(
    parent_pid: int,
    host: str,
    port: int,
    repo_dir: Path,
    update_id: str,
    relaunch_args: list[str],
) -> None:
    old_commit = get_git_commit(repo_dir)
    state_lock = threading.Lock()
    shared_state: dict[str, Any] = {
        "update_id": update_id,
        "phase": "closing",
        "ok": True,
        "message": "Waiting for server to close...",
        "old_commit": old_commit,
        "new_commit": old_commit,
        "git_output": "",
        "git_summary": "",
        "updated_at": time.time(),
    }
    write_update_state(shared_state)

    wait_for_server_close(parent_pid, host, port)

    with state_lock:
        shared_state.update(
            {
                "phase": "git_pull",
                "message": "Server closed. Running git pull...",
                "updated_at": time.time(),
            }
        )
    write_update_state(shared_state)

    temp_server = start_temporary_status_server(host, port, shared_state, state_lock)
    try:
        git_ok, git_output, git_summary = run_git_pull(repo_dir)
        new_commit = get_git_commit(repo_dir)
        with state_lock:
            shared_state.update(
                {
                    "phase": "relaunching",
                    "ok": git_ok,
                    "message": f"git pull complete ({git_summary}). Relaunching server...",
                    "new_commit": new_commit,
                    "git_output": git_output,
                    "git_summary": git_summary,
                    "updated_at": time.time(),
                }
            )
        write_update_state(shared_state)
        time.sleep(STATUS_HOLD_AFTER_PULL_S)
    finally:
        if temp_server is not None:
            try:
                temp_server.shutdown()
                temp_server.server_close()
            except Exception:
                pass

    # Mark final state as 'online' in the file so the new server reports it once booted
    with state_lock:
        final_file_state = dict(shared_state)
        final_file_state["phase"] = "online"
        final_file_state["message"] = (
            f"Server updated ({old_commit} -> {final_file_state['new_commit']}): "
            f"{final_file_state['git_summary']}"
        )
        final_file_state["updated_at"] = time.time()
    write_update_state(final_file_state)

    relaunch_main_server(relaunch_args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LocalWhisper detached server updater.")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-dir", default=str(REPO_ROOT))
    parser.add_argument("--update-id", required=True)
    parser.add_argument("--relaunch-args-json", default="[]")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        relaunch_args = json.loads(args.relaunch_args_json)
        if not isinstance(relaunch_args, list):
            relaunch_args = []
    except json.JSONDecodeError:
        relaunch_args = []

    execute_update_workflow(
        parent_pid=args.parent_pid,
        host=args.host,
        port=args.port,
        repo_dir=Path(args.repo_dir).resolve(),
        update_id=args.update_id,
        relaunch_args=[str(a) for a in relaunch_args],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
