"""Unit tests for server/handoff.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handoff import (  # noqa: E402
    DEFAULT_MIME,
    HandoffError,
    HandoffStore,
    clean_mime_type,
    sanitize_filename,
    save_upload,
)


class FakeUpload:
    """UploadFile stand-in: filename, content_type and an async read()."""

    def __init__(self, data: bytes, filename: str, content_type: str = "") -> None:
        self._data = data
        self._position = 0
        self.filename = filename
        self.content_type = content_type

    async def read(self, size: int) -> bytes:
        chunk = self._data[self._position : self._position + size]
        self._position += len(chunk)
        return chunk


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SanitizeTests(unittest.TestCase):
    def test_folders_are_removed(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Users\\x\\report.pdf"), "report.pdf")

    def test_unsafe_and_reserved_names(self) -> None:
        self.assertEqual(sanitize_filename('a<b>:c.txt'), "a_b__c.txt")
        self.assertEqual(sanitize_filename("con.txt"), "_con.txt")
        self.assertEqual(sanitize_filename("..."), "file")
        self.assertEqual(sanitize_filename(None), "file")

    def test_long_names_keep_their_extension(self) -> None:
        name = sanitize_filename("a" * 300 + ".jpeg")
        self.assertEqual(len(name), 120)
        self.assertTrue(name.endswith(".jpeg"))

    def test_mime_types(self) -> None:
        self.assertEqual(clean_mime_type("image/png; charset=binary", "x.png"), "image/png")
        self.assertEqual(clean_mime_type("not a type", "photo.jpg"), "image/jpeg")
        self.assertEqual(clean_mime_type("", "blob.zzqq"), DEFAULT_MIME)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "handoff"
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def store(self, max_file: int = 1024, max_total: int = 4096) -> HandoffStore:
        return HandoffStore(
            self.root, max_file_bytes=max_file, max_total_bytes=max_total, ttl_s=60.0, clock=self.clock
        )

    async def test_upload_round_trip(self) -> None:
        store = self.store()
        upload = FakeUpload(b"hello world", "../notes.txt", "text/plain")
        stored = await save_upload(store, upload, "phone", "pc")
        self.assertEqual((stored.name, stored.size, stored.mime_type), ("notes.txt", 11, "text/plain"))
        self.assertEqual((stored.source, stored.target), ("phone", "pc"))
        fetched = store.get(stored.file_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.data_path.read_bytes(), b"hello world")
        offer = stored.offer_payload()
        self.assertEqual(offer["type"], "file_offer")
        self.assertEqual(offer["url"], f"/api/files/{stored.file_id}")

    async def test_too_large_upload_is_rejected_and_cleaned_up(self) -> None:
        store = self.store(max_file=10, max_total=100)
        with self.assertRaises(HandoffError) as ctx:
            await save_upload(store, FakeUpload(b"x" * 11, "big.bin"), "pc", "phone")
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_empty_upload_is_rejected(self) -> None:
        store = self.store()
        with self.assertRaises(HandoffError):
            await save_upload(store, FakeUpload(b"", "empty.txt"), "pc", "phone")
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_files_expire(self) -> None:
        store = self.store()
        stored = await save_upload(store, FakeUpload(b"data", "a.txt"), "pc", "phone")
        self.clock.now += 61.0
        self.assertIsNone(store.get(stored.file_id))
        self.assertFalse(stored.data_path.exists())

    async def test_oldest_files_are_evicted_over_the_total_cap(self) -> None:
        store = self.store(max_file=10, max_total=20)
        ids: list[str] = []
        for index in range(3):
            self.clock.now += 1.0
            stored = await save_upload(store, FakeUpload(b"0123456789", f"f{index}.txt"), "pc", "phone")
            ids.append(stored.file_id)
        self.assertIsNone(store.get(ids[0]))
        self.assertIsNotNone(store.get(ids[1]))
        self.assertIsNotNone(store.get(ids[2]))

    def test_bad_ids_are_ignored(self) -> None:
        store = self.store()
        self.assertIsNone(store.get("../etc/passwd"))
        self.assertIsNone(store.get("abc"))
        self.assertIsNone(store.get(None))

    def test_limits_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            HandoffStore(self.root, max_file_bytes=10, max_total_bytes=5)


if __name__ == "__main__":
    unittest.main()
