"""Short-lived file handoff between devices: one side uploads, the other downloads within a day.

Files live in one folder as <id>.bin (data) + <id>.json (metadata). Uploads stream into <id>.part
first, so a half-finished upload is never served.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

LOGGER = logging.getLogger("localwhisper.handoff")

MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
TTL_S = 24 * 3600.0
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_STORED_FILES = 500
MAX_FILES_SCANNED = 2000
STALE_PARTIAL_S = 3600.0
MAX_NAME_CHARS = 120
MAX_EXTENSION_CHARS = 16
MAX_LABEL_CHARS = 32
MAX_MIME_CHARS = 100
FILE_ID_BYTES = 8
DATA_SUFFIX = ".bin"
META_SUFFIX = ".json"
PARTIAL_SUFFIX = ".part"
DEFAULT_NAME = "file"
DEFAULT_MIME = "application/octet-stream"
FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
MIME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
LABEL_UNSAFE_CHARS = re.compile(r"[^a-z0-9_-]")
RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class HandoffError(RuntimeError):
    """An upload or download that cannot be served; status_code is the HTTP status to report."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def sanitize_filename(raw: Any) -> str:
    """Base name only: no folders, control characters, Windows-reserved names or trailing dots."""
    base = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    clean = UNSAFE_NAME_CHARS.sub("_", base).strip().strip(".").strip()
    if not clean:
        return DEFAULT_NAME
    stem, extension = os.path.splitext(clean)
    extension = extension[:MAX_EXTENSION_CHARS]
    if stem.lower() in RESERVED_WINDOWS_NAMES:
        stem = "_" + stem
    stem = stem[: MAX_NAME_CHARS - len(extension)].rstrip(" .") or DEFAULT_NAME
    return stem + extension


def clean_mime_type(raw: Any, file_name: str) -> str:
    """A plain type/subtype from the uploader, else a guess from the file name."""
    candidate = str(raw or "").split(";", 1)[0].strip().lower()
    if candidate and len(candidate) <= MAX_MIME_CHARS and MIME_PATTERN.match(candidate):
        return candidate
    guessed, _encoding = mimetypes.guess_type(file_name)
    return guessed or DEFAULT_MIME


def clean_label(raw: Any) -> str:
    return LABEL_UNSAFE_CHARS.sub("", str(raw or "").strip().lower())[:MAX_LABEL_CHARS]


def _describe_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes // (1024 * 1024)} MB"
    return f"{size_bytes} bytes"


@dataclass(frozen=True)
class StoredFile:
    """A finished upload waiting for the other device."""

    file_id: str
    name: str
    mime_type: str
    size: int
    source: str
    target: str
    created_at: float
    expires_at: float
    data_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "source": self.source,
            "target": self.target,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "url": f"/api/files/{self.file_id}",
        }

    def offer_payload(self) -> dict[str, Any]:
        """One-way message telling the receiving device a file is ready to download."""
        return {"type": "file_offer", **self.to_dict()}


class UploadWriter:
    """Streams one upload into a .part file; commit() publishes it, abort() throws it away."""

    def __init__(
        self,
        store: HandoffStore,
        file_id: str,
        name: str,
        mime_type: str,
        source: str,
        target: str,
    ) -> None:
        self._store = store
        self.file_id = file_id
        self.name = name
        self.mime_type = mime_type
        self.source = source
        self.target = target
        self.partial_path = store.root / f"{file_id}{PARTIAL_SUFFIX}"
        self._handle: BinaryIO | None = open(self.partial_path, "wb")  # noqa: SIM115 - closed in commit/abort
        self._written = 0

    @property
    def max_chunks(self) -> int:
        """Upper bound on read() calls for an upload within the limit (plus the final empty read)."""
        return self._store.max_file_bytes // UPLOAD_CHUNK_BYTES + 2

    def write(self, chunk: bytes) -> None:
        if self._handle is None:
            raise HandoffError("This upload is already finished")
        limit = self._store.max_file_bytes
        if self._written + len(chunk) > limit:
            raise HandoffError(f"File is larger than the {_describe_size(limit)} limit", status_code=413)
        self._handle.write(chunk)
        self._written += len(chunk)

    def commit(self) -> StoredFile:
        if self._handle is None:
            raise HandoffError("This upload is already finished")
        self._close()
        if self._written <= 0:
            self._delete_partial()
            raise HandoffError("File is empty")
        return self._store.publish(self, self._written)

    def abort(self) -> None:
        self._close()
        self._delete_partial()

    def _close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def _delete_partial(self) -> None:
        try:
            self.partial_path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("Could not delete partial upload %s: %s", self.partial_path, exc)


class HandoffStore:
    """Keeps uploaded files on disk for a limited time and total size."""

    def __init__(
        self,
        root: Path,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        ttl_s: float = TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_file_bytes <= 0 or max_total_bytes < max_file_bytes:
            raise ValueError("Limits must be positive and max_total_bytes at least max_file_bytes")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self.root = Path(root)
        self.max_file_bytes = int(max_file_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.ttl_s = float(ttl_s)
        self._clock = clock

    def begin(self, name: Any, mime_type: Any, source: Any = "", target: Any = "") -> UploadWriter:
        """Starts an upload. Blocking file I/O: call it from a worker thread."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup()
        clean_name = sanitize_filename(name)
        return UploadWriter(
            store=self,
            file_id=secrets.token_hex(FILE_ID_BYTES),
            name=clean_name,
            mime_type=clean_mime_type(mime_type, clean_name),
            source=clean_label(source),
            target=clean_label(target),
        )

    def publish(self, writer: UploadWriter, size: int) -> StoredFile:
        """Moves a finished upload into place and records it (called by UploadWriter.commit)."""
        now = self._clock()
        stored = StoredFile(
            file_id=writer.file_id,
            name=writer.name,
            mime_type=writer.mime_type,
            size=int(size),
            source=writer.source,
            target=writer.target,
            created_at=now,
            expires_at=now + self.ttl_s,
            data_path=self._data_path(writer.file_id),
        )
        try:
            os.replace(writer.partial_path, stored.data_path)
            self._write_meta(stored)
        except OSError as exc:
            self._delete(stored.file_id)
            raise HandoffError(f"Could not store the file: {exc}", status_code=500) from exc
        self.cleanup()
        return stored

    def get(self, file_id: Any) -> StoredFile | None:
        """The stored file, or None when the id is invalid, unknown or expired."""
        clean_id = str(file_id or "").strip().lower()
        if not FILE_ID_PATTERN.match(clean_id):
            return None
        stored = self._read_meta(self._meta_path(clean_id))
        if stored is None:
            return None
        if stored.expires_at <= self._clock():
            self._delete(clean_id)
            return None
        return stored

    def cleanup(self) -> int:
        """Deletes expired files, stale partial uploads, and the oldest files over the size or count caps."""
        if not self.root.is_dir():
            return 0
        now = self._clock()
        alive: list[StoredFile] = []
        removed = 0
        for stored in self._list_stored():
            if stored.expires_at <= now:
                self._delete(stored.file_id)
                removed += 1
            else:
                alive.append(stored)
        removed += self._evict_over_caps(alive)
        removed += self._remove_stale_partials(now)
        return removed

    def _evict_over_caps(self, alive: list[StoredFile]) -> int:
        ordered = sorted(alive, key=lambda stored: stored.created_at)
        total = sum(stored.size for stored in ordered)
        count = len(ordered)
        removed = 0
        for stored in ordered:
            if total <= self.max_total_bytes and count <= MAX_STORED_FILES:
                break
            self._delete(stored.file_id)
            total -= stored.size
            count -= 1
            removed += 1
        return removed

    def _remove_stale_partials(self, now: float) -> int:
        removed = 0
        for path in list(itertools.islice(self.root.glob(f"*{PARTIAL_SUFFIX}"), MAX_FILES_SCANNED)):
            try:
                if now - path.stat().st_mtime > STALE_PARTIAL_S:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as exc:
                LOGGER.warning("Could not clean up partial upload %s: %s", path, exc)
        return removed

    def _list_stored(self) -> list[StoredFile]:
        stored_files: list[StoredFile] = []
        for meta_path in list(itertools.islice(self.root.glob(f"*{META_SUFFIX}"), MAX_FILES_SCANNED)):
            stored = self._read_meta(meta_path)
            if stored is not None:
                stored_files.append(stored)
            elif FILE_ID_PATTERN.match(meta_path.stem):
                self._delete(meta_path.stem)  # metadata without data, or unreadable metadata
        return stored_files

    def _read_meta(self, meta_path: Path) -> StoredFile | None:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        file_id = str(data.get("id", ""))
        data_path = self._data_path(file_id)
        if not FILE_ID_PATTERN.match(file_id) or meta_path.stem != file_id or not data_path.is_file():
            return None
        try:
            return StoredFile(
                file_id=file_id,
                name=sanitize_filename(data.get("name")),
                mime_type=clean_mime_type(data.get("mime_type"), str(data.get("name", ""))),
                size=int(data.get("size", 0)),
                source=clean_label(data.get("source")),
                target=clean_label(data.get("target")),
                created_at=float(data.get("created_at", 0.0)),
                expires_at=float(data.get("expires_at", 0.0)),
                data_path=data_path,
            )
        except (TypeError, ValueError):
            return None

    def _write_meta(self, stored: StoredFile) -> None:
        meta = stored.to_dict()
        meta.pop("url", None)
        meta_path = self._meta_path(stored.file_id)
        temp_path = meta_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(temp_path, meta_path)

    def _delete(self, file_id: str) -> None:
        for path in (self._data_path(file_id), self._meta_path(file_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning("Could not delete handoff file %s: %s", path, exc)

    def _data_path(self, file_id: str) -> Path:
        return self.root / f"{file_id}{DATA_SUFFIX}"

    def _meta_path(self, file_id: str) -> Path:
        return self.root / f"{file_id}{META_SUFFIX}"


async def save_upload(store: HandoffStore, upload: Any, source: str, target: str) -> StoredFile:
    """Streams an UploadFile-like object (filename, content_type, async read) into the store."""
    writer = await asyncio.to_thread(
        store.begin,
        getattr(upload, "filename", "") or "",
        getattr(upload, "content_type", "") or "",
        source,
        target,
    )
    try:
        for _read_index in range(writer.max_chunks):
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                return await asyncio.to_thread(writer.commit)
            await asyncio.to_thread(writer.write, chunk)
        raise HandoffError(
            f"File is larger than the {_describe_size(store.max_file_bytes)} limit", status_code=413
        )
    except BaseException:
        writer.abort()
        raise
