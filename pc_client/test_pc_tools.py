"""Unit tests for pc_tools. Run: python -m unittest discover -s pc_client -p "test_*.py" -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pc_tools import (  # noqa: E402
    build_media_ahk_token,
    run_ahk_sequence,
    validate_http_url,
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


if __name__ == "__main__":
    unittest.main()
