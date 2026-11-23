"""Unit tests for pc_tools. Run: python -m unittest discover -s pc_client -p "test_*.py" -v"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pc_tools import (  # noqa: E402
    SCREENSHOT_MAX_WIDTH,
    SCREENSHOT_MIN_WIDTH,
    build_media_ahk_token,
    filter_windows_list,
    run_ahk_sequence,
    run_capture_pc_screenshot,
    run_inspect_pc_screen,
    run_read_pc_image,
    validate_http_url,
    validate_image_file_path,
)


class MediaTokenTests(unittest.TestCase):
    def test_play_pause_token(self) -> None:
        self.assertEqual(build_media_ahk_token("play_pause"), "{Media_Play_Pause}")

    def test_volume_up_clamps_steps(self) -> None:
        self.assertEqual(build_media_ahk_token("volume_up", 3), "{Volume_Up 3}")
        self.assertEqual(build_media_ahk_token("volume_down", 99), "{Volume_Down 20}")

    def test_invalid_media_action_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_media_ahk_token("invalid_action")


class UrlValidationTests(unittest.TestCase):
    def test_valid_https_url(self) -> None:
        self.assertEqual(validate_http_url("https://pytorch.org"), "https://pytorch.org")

    def test_adds_https_scheme_when_missing(self) -> None:
        self.assertEqual(validate_http_url("github.com"), "https://github.com")

    def test_rejects_non_http_schemes(self) -> None:
        with self.assertRaises(ValueError):
            validate_http_url("file:///C:/Windows/System32/cmd.exe")


class AhkSequenceTests(unittest.TestCase):
    def test_executes_multi_step_sequence_in_order(self) -> None:
        sent: list[str] = []

        def fake_sender(_exe: Path, text: str, _mode: str, _human: bool) -> None:
            sent.append(text)

        result = run_ahk_sequence(
            {
                "steps": [
                    {"text": "{^a}", "wait_ms": 0},
                    {"text": "SELECT 1;", "wait_ms": 0},
                    {"text": "{^Enter}", "wait_ms": 0},
                ]
            },
            Path("AutoHotkeyU64.exe"),
            "Input",
            False,
            fake_sender,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps_executed"], 3)
        self.assertEqual(sent, ["{^a}", "SELECT 1;", "{^Enter}"])

    def test_empty_steps_raises(self) -> None:
        with self.assertRaises(ValueError):
            run_ahk_sequence(
                {"steps": []},
                Path("AutoHotkeyU64.exe"),
                "Input",
                False,
                lambda *_: None,
            )


class WindowListFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_windows = [
            {
                "hwnd": "0x101",
                "pid": 1200,
                "process_name": "Code.exe",
                "title": "main.py - LocalWhisper - Visual Studio Code",
                "class_name": "Chrome_WidgetWin_1",
                "is_foreground": True,
                "state": "maximized",
            },
            {
                "hwnd": "0x202",
                "pid": 3400,
                "process_name": "chrome.exe",
                "title": "PyTorch Documentation - Google Chrome",
                "class_name": "Chrome_WidgetWin_1",
                "is_foreground": False,
                "state": "minimized",
            },
        ]

    def test_filters_by_query_tokens(self) -> None:
        matched = filter_windows_list(self.sample_windows, query="chrome pytorch")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["pid"], 3400)

    def test_excludes_minimized_when_requested(self) -> None:
        matched = filter_windows_list(self.sample_windows, query="", include_minimized=False)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["process_name"], "Code.exe")


class ImageToolsTests(unittest.TestCase):
    def test_validate_image_file_path_rejects_bad_extensions_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_path = Path(tmp_dir) / "notes.txt"
            txt_path.write_text("hello", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_image_file_path(str(txt_path))
            with self.assertRaises(ValueError):
                validate_image_file_path(str(Path(tmp_dir) / "missing.png"))
            empty_png = Path(tmp_dir) / "empty.png"
            empty_png.write_bytes(b"")
            with self.assertRaises(ValueError):
                validate_image_file_path(str(empty_png))
            valid_png = Path(tmp_dir) / "chart.png"
            valid_png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            self.assertEqual(validate_image_file_path(str(valid_png)), valid_png.resolve())

    def test_run_capture_pc_screenshot_returns_jpeg_and_clamps_width(self) -> None:
        fake_windows = [
            {
                "hwnd": "0x101",
                "_hwnd_int": 257,
                "process_name": "chrome.exe",
                "title": "Dashboard - Google Chrome",
                "is_foreground": True,
                "state": "normal",
                "bounds": {"x": 10, "y": 20, "width": 1200, "height": 800},
            }
        ]
        captured_params: list[dict[str, object]] = []

        def fake_script(mode: str, params: dict[str, object]):
            captured_params.append({"mode": mode, **params})
            if mode == "ocr":
                return {"ok": True, "lines": [{"text": "Dashboard", "x": 20, "y": 30, "w": 90, "h": 20}]}, "ok"
            Path(str(params["OutPath"])).write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")
            return {"ok": True, "width": 640, "height": 400}, "ok"

        with (
            mock.patch("pc_tools._enumerate_win32_windows", return_value=(fake_windows, "0x101")),
            mock.patch("pc_tools._run_screen_script", side_effect=fake_script),
        ):
            result = run_capture_pc_screenshot({"window_query": "chrome", "max_width": 99999})

        self.assertTrue(result["ok"])
        self.assertEqual(result["mime_type"], "image/jpeg")
        self.assertEqual(result["width"], 640)
        self.assertEqual(base64.b64decode(result["image_jpeg_b64"]), b"\xff\xd8\xff\xe0FAKEJPEG")
        self.assertEqual(result["ocr_status"], "ok")
        self.assertIn("Dashboard", result["elements"])
        self.assertEqual(captured_params[0]["mode"], "screenshot")
        self.assertEqual(captured_params[0]["MaxWidth"], SCREENSHOT_MAX_WIDTH)
        self.assertEqual(captured_params[1]["mode"], "ocr")

    def test_run_inspect_pc_screen_returns_both_ocr_and_visual_jpeg(self) -> None:
        fake_windows = [
            {
                "hwnd": "0x101",
                "_hwnd_int": 257,
                "process_name": "chrome.exe",
                "title": "Inbox - Google Chrome",
                "is_foreground": True,
                "state": "normal",
                "bounds": {"x": 10, "y": 20, "width": 1200, "height": 800},
            }
        ]

        def fake_script(mode: str, params: dict[str, object]):
            if mode == "ocr":
                return {"ok": True, "lines": [{"text": "Compose", "x": 30, "y": 50, "w": 80, "h": 22}]}, "ok"
            Path(str(params["OutPath"])).write_bytes(b"\xff\xd8\xff\xe0SCREEN")
            return {"ok": True, "width": 960, "height": 640}, "ok"

        with (
            mock.patch("pc_tools._enumerate_win32_windows", return_value=(fake_windows, "0x101")),
            mock.patch("pc_tools._run_screen_script", side_effect=fake_script),
        ):
            result = run_inspect_pc_screen({"window_query": "chrome"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["ocr_status"], "ok")
        self.assertEqual(result["screenshot_status"], "ok")
        self.assertIn("Compose", result["elements"])
        self.assertEqual(base64.b64decode(result["image_jpeg_b64"]), b"\xff\xd8\xff\xe0SCREEN")

    def test_run_capture_pc_screenshot_rejects_minimized_target_window(self) -> None:
        fake_windows = [
            {
                "hwnd": "0x202",
                "_hwnd_int": 514,
                "process_name": "chrome.exe",
                "title": "Hidden - Google Chrome",
                "is_foreground": False,
                "state": "minimized",
                "bounds": {"x": 0, "y": 0, "width": 0, "height": 0},
            }
        ]
        with mock.patch("pc_tools._enumerate_win32_windows", return_value=(fake_windows, None)):
            result = run_capture_pc_screenshot({"window_query": "chrome"})
        self.assertFalse(result["ok"])
        self.assertIn("target_minimized", result["error"])

    def test_run_read_pc_image_encodes_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_path = Path(tmp_dir) / "sample.jpg"
            img_path.write_bytes(b"\xff\xd8\xff\xe0RAW")
            captured_params: list[dict[str, object]] = []

            def fake_script(mode: str, params: dict[str, object]):
                captured_params.append({"mode": mode, **params})
                Path(str(params["OutPath"])).write_bytes(b"\xff\xd8\xff\xe0RESIZED")
                return {"ok": True, "width": 320, "height": 240}, "ok"

            with mock.patch("pc_tools._run_screen_script", side_effect=fake_script):
                result = run_read_pc_image({"path": str(img_path), "max_width": 50})

        self.assertTrue(result["ok"])
        self.assertEqual(result["file_name"], "sample.jpg")
        self.assertEqual(base64.b64decode(result["image_jpeg_b64"]), b"\xff\xd8\xff\xe0RESIZED")
        self.assertEqual(captured_params[0]["mode"], "load_image")
        self.assertEqual(captured_params[0]["MaxWidth"], SCREENSHOT_MIN_WIDTH)


if __name__ == "__main__":
    unittest.main()
