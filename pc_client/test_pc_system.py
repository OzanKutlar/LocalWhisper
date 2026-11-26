"""Unit tests for pure helpers in pc_client/pc_system.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pc_system import (  # noqa: E402
    clamp_percentage,
    match_audio_device,
    parse_nvidia_smi_csv,
    validate_power_action,
)


class PcSystemHelperTests(unittest.TestCase):
    def test_parse_nvidia_smi_csv(self) -> None:
        sample = "NVIDIA GeForce RTX 5060 Ti, 48, 12, 3450, 16384\n"
        gpus = parse_nvidia_smi_csv(sample)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 5060 Ti")
        self.assertEqual(gpus[0]["temperature_c"], 48.0)
        self.assertEqual(gpus[0]["utilization_pct"], 12.0)
        self.assertEqual(gpus[0]["vram_used_mb"], 3450.0)
        self.assertEqual(gpus[0]["vram_total_mb"], 16384.0)

    def test_parse_nvidia_smi_handles_empty_and_malformed(self) -> None:
        self.assertEqual(parse_nvidia_smi_csv(""), [])
        self.assertEqual(parse_nvidia_smi_csv("bad line"), [])

    def test_match_audio_device_exact_substring_and_ambiguous(self) -> None:
        devices = [
            {"id": "1", "name": "Speakers (Realtek USB Audio)"},
            {"id": "2", "name": "Headphones (Sony WH-1000XM5)"},
            {"id": "3", "name": "Headset Earphone (Sony WH-1000XM5)"},
        ]
        matched, cands = match_audio_device(devices, "realtek")
        self.assertEqual(matched["id"], "1")
        self.assertEqual(cands, [])

        matched_ambig, cands_ambig = match_audio_device(devices, "sony")
        self.assertIsNone(matched_ambig)
        self.assertEqual(len(cands_ambig), 2)

        matched_tokens, _c = match_audio_device(devices, "headphones xm5")
        self.assertEqual(matched_tokens["id"], "2")

    def test_validate_power_action_and_delay_clamping(self) -> None:
        self.assertEqual(validate_power_action("sleep"), ("sleep", 30))
        self.assertEqual(validate_power_action("shutdown", 99999), ("shutdown", 3600))
        self.assertEqual(validate_power_action("cancel_shutdown", -5), ("cancel_shutdown", 0))
        with self.assertRaises(ValueError):
            validate_power_action("explode")

    def test_clamp_percentage(self) -> None:
        self.assertEqual(clamp_percentage(45), 45)
        self.assertEqual(clamp_percentage(-10), 0)
        self.assertEqual(clamp_percentage(150), 100)
        with self.assertRaises(ValueError):
            clamp_percentage(True)
        with self.assertRaises(ValueError):
            clamp_percentage("loud")


if __name__ == "__main__":
    unittest.main()
