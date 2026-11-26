"""Unit tests for pc_client/handoff_client.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handoff_client import (  # noqa: E402
    handle_pushed_text,
    http_base_from_ws,
    is_single_http_url,
    sanitize_local_filename,
    unique_destination_path,
)


class HandoffClientTests(unittest.TestCase):
    def test_http_base_from_ws(self) -> None:
        self.assertEqual(http_base_from_ws("ws://192.168.1.10:8765"), "http://192.168.1.10:8765")
        self.assertEqual(http_base_from_ws("wss://host/ws/client"), "https://host")
        self.assertEqual(http_base_from_ws("desktop:8765"), "http://desktop:8765")

    def test_unique_destination_avoids_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = unique_destination_path(root, "photo.png")
            self.assertEqual(first.name, "photo.png")
            first.write_bytes(b"1")
            second = unique_destination_path(root, "photo.png")
            self.assertEqual(second.name, "photo (1).png")
            second.write_bytes(b"2")
            third = unique_destination_path(root, "photo.png")
            self.assertEqual(third.name, "photo (2).png")

    def test_sanitize_local_filename_strips_paths(self) -> None:
        self.assertEqual(sanitize_local_filename("../../evil.exe"), "evil.exe")
        self.assertEqual(sanitize_local_filename("..."), "file")

    def test_handle_pushed_text_routes_links_vs_plain_text(self) -> None:
        opened: list[str] = []
        copied: list[str] = []

        def fake_copy(text: str) -> dict[str, object]:
            copied.append(text)
            return {"ok": True}

        def fake_open(url: str) -> bool:
            opened.append(url)
            return True

        res_url = handle_pushed_text("https://example.com/docs", fake_copy, fake_open)
        self.assertEqual(res_url["action"], "opened_url")
        self.assertEqual(opened, ["https://example.com/docs"])
        self.assertEqual(copied, [])

        res_txt = handle_pushed_text("check https://example.com later", fake_copy, fake_open)
        self.assertEqual(res_txt["action"], "copied_to_clipboard")
        self.assertEqual(copied, ["check https://example.com later"])
        self.assertFalse(is_single_http_url("file:///C:/Windows/cmd.exe"))


if __name__ == "__main__":
    unittest.main()
