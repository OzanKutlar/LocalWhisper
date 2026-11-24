"""Unit tests for server/llm_asr_engine.py."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asr_types import MAX_AUDIO_BYTES  # noqa: E402
from llm_asr_engine import LlmAsrEngine, clean_transcript  # noqa: E402

WAV = b"RIFF" + bytes(60)


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


class Recorder:
    """Fake llama-server: returns queued responses and records request URLs and JSON bodies."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0) if self.responses else httpx.Response(500)

    def engine(self) -> LlmAsrEngine:
        return LlmAsrEngine(
            base_url="http://127.0.0.1:34501/v1",
            model="llama-server",
            display_name="gemma-3n.gguf",
            transport=httpx.MockTransport(self.handler),
        )


class RequestShapeTests(unittest.TestCase):
    def test_sends_wav_as_input_audio_with_language_hint(self) -> None:
        recorder = Recorder([completion("Merhaba dünya")])
        result = recorder.engine().transcribe_bytes(WAV, language=" Turkish ")

        self.assertEqual(result.text, "Merhaba dünya")
        self.assertEqual(result.language, "Turkish")
        self.assertEqual(recorder.urls, ["http://127.0.0.1:34501/v1/chat/completions"])
        body = recorder.bodies[0]
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        parts = body["messages"][1]["content"]
        self.assertIn("Turkish", parts[0]["text"])
        audio = parts[1]["input_audio"]
        self.assertEqual(audio["format"], "wav")
        self.assertEqual(base64.b64decode(audio["data"]), WAV)

    def test_retries_without_template_kwargs_after_400(self) -> None:
        recorder = Recorder([httpx.Response(400, text="bad kwarg"), completion("ok")])
        result = recorder.engine().transcribe_bytes(WAV)
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(recorder.bodies), 2)
        self.assertNotIn("chat_template_kwargs", recorder.bodies[1])

    def test_server_error_raises_runtime_error(self) -> None:
        recorder = Recorder([httpx.Response(500, text="boom")])
        with self.assertRaises(RuntimeError):
            recorder.engine().transcribe_bytes(WAV)

    def test_rejects_audio_outside_size_limits(self) -> None:
        engine = Recorder([]).engine()
        with self.assertRaises(ValueError):
            engine.transcribe_bytes(b"RIFF")
        with self.assertRaises(ValueError):
            engine.transcribe_bytes(bytes(MAX_AUDIO_BYTES + 1))

    def test_model_id_names_llama_server(self) -> None:
        engine = Recorder([]).engine()
        self.assertEqual(engine.model_id, "gemma-3n.gguf (llama-server audio)")
        self.assertEqual(engine.device, "llama-server")


class CleanTranscriptTests(unittest.TestCase):
    def test_strips_think_blocks_and_whitespace(self) -> None:
        self.assertEqual(clean_transcript("<think>hmm</think>\n  Hello there.  "), "Hello there.")
        self.assertEqual(clean_transcript("stray reasoning</think>Hi"), "Hi")

    def test_strips_wrapping_quotes_only(self) -> None:
        self.assertEqual(clean_transcript('"Hello."'), "Hello.")
        self.assertEqual(clean_transcript("“Hello.”"), "Hello.")
        self.assertEqual(clean_transcript('He said "hi"'), 'He said "hi"')


if __name__ == "__main__":
    unittest.main()
