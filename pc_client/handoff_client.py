"""PC-side file and clipboard/link handoff through the LocalWhisper server."""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

LOGGER = logging.getLogger("localwhisper.handoff_client")

MAX_FILE_BYTES = 200 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 256 * 1024
TRANSFER_TIMEOUT_S = 120.0
MAX_UNIQUE_ATTEMPTS = 999
MAX_FILENAME_CHARS = 120
UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def http_base_from_ws(server_url: str) -> str:
    """Converts a server ws://, wss://, http://, or bare host:port string to an HTTP(S) base URL."""
    cleaned = str(server_url or "").strip().rstrip("/")
    if not cleaned:
        raise ValueError("server_url must not be empty")
    if cleaned.endswith("/ws/client"):
        cleaned = cleaned[: -len("/ws/client")].rstrip("/")
    if cleaned.startswith("ws://"):
        return "http://" + cleaned[len("ws://") :]
    if cleaned.startswith("wss://"):
        return "https://" + cleaned[len("wss://") :]
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return "http://" + cleaned


def sanitize_local_filename(raw: Any) -> str:
    base = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    clean = UNSAFE_CHARS.sub("_", base).strip().strip(".").strip()
    if not clean:
        return "file"
    stem, ext = os.path.splitext(clean)
    ext = ext[:16]
    stem = stem[: MAX_FILENAME_CHARS - len(ext)].rstrip(" .") or "file"
    return stem + ext


def unique_destination_path(directory: Path, filename: str) -> Path:
    """Returns a path inside directory that does not overwrite an existing file."""
    safe_name = sanitize_local_filename(filename)
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(safe_name)
    for index in range(1, MAX_UNIQUE_ATTEMPTS + 1):
        numbered = directory / f"{stem} ({index}){ext}"
        if not numbered.exists():
            return numbered
    return directory / f"{stem}_{secrets.token_hex(4)}{ext}"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "LocalWhisper"


def is_single_http_url(text: str) -> bool:
    """True when text is a single-line http:// or https:// URL with a host."""
    stripped = str(text or "").strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    parsed = urlparse(stripped)
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def handle_pushed_text(
    text: str,
    write_clipboard_fn: Callable[[str], dict[str, Any]],
    open_url_fn: Callable[[str], bool],
) -> dict[str, Any]:
    """Opens a single-line HTTP(S) link in the browser; copies anything else to the clipboard."""
    if is_single_http_url(text):
        url = text.strip()
        opened = bool(open_url_fn(url))
        return {"ok": opened, "action": "opened_url", "url": url}
    result = write_clipboard_fn(text)
    return {"ok": bool(result.get("ok", True)), "action": "copied_to_clipboard", "length": len(text)}


def download_handoff_file(
    server_http_base: str,
    offer: dict[str, Any],
    dest_dir: Path | None = None,
) -> Path:
    """Downloads an offered file into Downloads\\LocalWhisper without overwriting."""
    import httpx  # noqa: PLC0415

    rel_url = str(offer.get("url", "") or "").strip()
    if not rel_url.startswith("/api/files/"):
        raise ValueError(f"Invalid file offer URL: {rel_url}")
    name = sanitize_local_filename(offer.get("name", "file"))
    target_dir = dest_dir or default_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = unique_destination_path(target_dir, name)
    part_path = target_dir / f".{final_path.name}.{secrets.token_hex(4)}.part"

    full_url = server_http_base.rstrip("/") + rel_url
    written = 0
    try:
        with httpx.Client(timeout=TRANSFER_TIMEOUT_S) as client:
            with client.stream("GET", full_url) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Download failed (HTTP {response.status_code})")
                with open(part_path, "wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_FILE_BYTES:
                            raise RuntimeError("Downloaded file exceeds the 200 MB limit")
                        handle.write(chunk)
        os.replace(part_path, final_path)
    except Exception:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    LOGGER.info("Saved handoff file (%d bytes) to %s", written, final_path)
    return final_path


def upload_handoff_file(
    server_http_base: str,
    raw_path: str,
    target: str = "phone",
    source: str = "pc",
) -> dict[str, Any]:
    """Uploads a local PC file to the server handoff store for the phone."""
    import httpx  # noqa: PLC0415

    cleaned = str(raw_path or "").strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("File path must not be empty")
    resolved = Path(os.path.expandvars(os.path.expanduser(cleaned)))
    if not resolved.is_file():
        raise ValueError(f"File not found on PC: {cleaned}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError("File is empty")
    if size > MAX_FILE_BYTES:
        raise ValueError("File exceeds the 200 MB handoff limit")
    if not server_http_base:
        raise RuntimeError("PC client does not know the server HTTP URL")

    url = server_http_base.rstrip("/") + "/api/files"
    with open(resolved, "rb") as handle:
        files = {"file": (resolved.name, handle, "application/octet-stream")}
        data = {"target": target, "source": source}
        with httpx.Client(timeout=TRANSFER_TIMEOUT_S) as client:
            response = client.post(url, files=files, data=data)
    if response.status_code >= 400:
        raise RuntimeError(f"Upload failed (HTTP {response.status_code}): {response.text[:200]}")
    payload = response.json()
    return {
        "ok": True,
        "id": payload.get("id", ""),
        "name": payload.get("name", resolved.name),
        "size": payload.get("size", size),
        "delivered": bool(payload.get("delivered", False)),
        "url": payload.get("url", ""),
    }
