"""Unit tests for server/llm_capabilities.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_capabilities import (  # noqa: E402
    LlmCapabilities,
    choose_asr_backend,
    parse_props,
    probe_llm_capabilities,
    server_root_from_base_url,
)

BASE_URL = "http://127.0.0.1:34501/v1"
PROPS_AUDIO = {
    "model_path": "C:/models/gemma-3n-E4B.gguf",
    "modalities": {"vision": True, "audio": True},
}


class ScriptedTransport:
    """Returns queued responses (or raises queued exceptions) in order and records request URLs."""

    def __init__(self, items: list[object]) -> None:
        self.items = list(items)
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        item = self.items.pop(0) if self.items else httpx.Response(500)
        if isinstance(item, Exception):
            raise item
        return item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class RootUrlTests(unittest.TestCase):
    def test_strips_v1_suffix(self) -> None:
        self.assertEqual(server_root_from_base_url(BASE_URL), "http://127.0.0.1:34501")
        self.assertEqual(server_root_from_base_url(BASE_URL + "/"), "http://127.0.0.1:34501")
        self.assertEqual(server_root_from_base_url("http://host:1"), "http://host:1")

    def test_empty_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            server_root_from_base_url("  ")


class ParsePropsTests(unittest.TestCase):
    def test_audio_and_vision(self) -> None:
        caps = parse_props(PROPS_AUDIO)
        self.assertTrue(caps.reachable)
        self.assertTrue(caps.audio)
        self.assertTrue(caps.vision)
        self.assertEqual(caps.model_name, "gemma-3n-E4B.gguf")

    def test_vision_only(self) -> None:
        caps = parse_props({"modalities": {"vision": True, "audio": False}})
        self.assertTrue(caps.vision)
        self.assertFalse(caps.audio)

    def test_missing_modalities_means_unsupported(self) -> None:
        caps = parse_props({"model_path": "x.gguf"})
        self.assertFalse(caps.audio)
        self.assertIn("modalities", caps.detail)

    def test_non_bool_and_garbage_payloads(self) -> None:
        self.assertFalse(parse_props({"modalities": {"audio": "yes"}}).audio)
        self.assertFalse(parse_props([1, 2]).audio)


class ProbeTests(unittest.TestCase):
    def test_retries_through_503_then_succeeds(self) -> None:
        scripted = ScriptedTransport([httpx.Response(503), httpx.Response(200, json=PROPS_AUDIO)])
        caps = probe_llm_capabilities(BASE_URL, delay_s=0, transport=scripted.transport())
        self.assertTrue(caps.audio)
        self.assertEqual(scripted.urls, ["http://127.0.0.1:34501/props"] * 2)

    def test_unreachable_server_gives_up_after_bounded_attempts(self) -> None:
        scripted = ScriptedTransport([httpx.ConnectError("refused")] * 5)
        caps = probe_llm_capabilities(BASE_URL, attempts=2, delay_s=0, transport=scripted.transport())
        self.assertFalse(caps.reachable)
        self.assertEqual(len(scripted.urls), 2)

    def test_http_error_is_not_retried(self) -> None:
        scripted = ScriptedTransport([httpx.Response(404)])
        caps = probe_llm_capabilities(BASE_URL, delay_s=0, transport=scripted.transport())
        self.assertTrue(caps.reachable)
        self.assertFalse(caps.audio)
        self.assertEqual(len(scripted.urls), 1)


class ChooseBackendTests(unittest.TestCase):
    AUDIO = LlmCapabilities(reachable=True, audio=True)
    NO_AUDIO = LlmCapabilities(reachable=True, vision=True)
    DOWN = LlmCapabilities(reachable=False, detail="refused")

    def test_auto_follows_audio_support(self) -> None:
        self.assertEqual(choose_asr_backend("auto", self.AUDIO), "llm")
        self.assertEqual(choose_asr_backend("auto", self.NO_AUDIO), "local")
        self.assertEqual(choose_asr_backend("auto", self.DOWN), "local")

    def test_local_never_uses_llm(self) -> None:
        self.assertEqual(choose_asr_backend("local", self.AUDIO), "local")

    def test_forced_llm_without_audio_raises(self) -> None:
        self.assertEqual(choose_asr_backend("llm", self.AUDIO), "llm")
        with self.assertRaises(RuntimeError):
            choose_asr_backend("llm", self.NO_AUDIO)


if __name__ == "__main__":
    unittest.main()
