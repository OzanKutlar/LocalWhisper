"""Unit tests for pc_client/overlay/mic.py and hotkey.py."""

from __future__ import annotations

from array import array
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from overlay.hotkey import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, parse_hotkey  # noqa: E402
from overlay.mic import (  # noqa: E402
    SilenceEndpointDetector,
    compute_rms_level,
    encode_pcm16_wav,
)


class MicAndHotkeyTests(unittest.TestCase):
    def test_compute_rms_and_encode_wav(self) -> None:
        silence = array("h", [0] * 800).tobytes()
        self.assertEqual(compute_rms_level(silence), 0.0)
        loud = array("h", [16384] * 800).tobytes()
        self.assertAlmostEqual(compute_rms_level(loud), 0.5, places=2)

        wav = encode_pcm16_wav(loud)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(len(wav), 44 + len(loud))

    def test_silence_detector_waits_for_speech_then_stops_after_pause(self) -> None:
        det = SilenceEndpointDetector(auto_stop=True, silence_duration_s=0.2, initial_wait_s=1.0, chunk_ms=50)
        # 4 chunks of initial silence (0.2s < 1.0s): should not stop
        for _ in range(4):
            self.assertFalse(det.feed(0.001))
        # Speech arrives
        self.assertFalse(det.feed(0.08))
        self.assertTrue(det.heard_speech)
        # 3 quiet chunks (0.15s) then 4th quiet chunk (0.20s) triggers stop
        for _ in range(3):
            self.assertFalse(det.feed(0.002))
        self.assertTrue(det.feed(0.002))

    def test_parse_hotkey(self) -> None:
        mods, vk = parse_hotkey("Ctrl+Alt+Space")
        self.assertEqual(mods, MOD_NOREPEAT | MOD_CONTROL | MOD_ALT)
        self.assertEqual(vk, 0x20)
        with self.assertRaises(ValueError):
            parse_hotkey("Ctrl+Shift")


if __name__ == "__main__":
    unittest.main()
