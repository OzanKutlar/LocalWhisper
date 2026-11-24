"""Transcribes audio with a llama-server model that accepts audio input.

The WAV is sent to the OpenAI-compatible /chat/completions endpoint as an input_audio part.
The class mirrors AsrEngine (model_id, device, gpu_name, load, transcribe_bytes) so the
FastAPI app can use either one.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

import httpx

from asr_types import TranscriptionResult, validate_audio_bytes

LOGGER = logging.getLogger("localwhisper.llm_asr")

LLM_ASR_TIMEOUT_S = 120.0
MAX_LANGUAGE_CHARS = 40
MAX_ERROR_DETAIL_CHARS = 300
THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
CLOSE_THINK_PATTERN = re.compile(r"</think>", re.IGNORECASE)
QUOTE_PAIRS = (("\"", "\""), ("“", "”"))
TRANSCRIBE_SYSTEM_PROMPT = (
    "You are a speech-to-text transcriber. Transcribe the user's audio exactly as spoken, "
    "in the language it is spoken in. Output only the transcript text: no commentary, "
    "no labels, no quotation marks, no translation."
)


def clean_language_hint(language: str | None) -> str:
    if not language:
        return ""
    return " ".join(str(language).split())[:MAX_LANGUAGE_CHARS]


def build_transcription_messages(audio_bytes: bytes, language: str) -> list[dict[str, Any]]:
    """System prompt plus a user message carrying the WAV as an input_audio part."""
    instruction = "Transcribe this audio."
    if language:
        instruction += f" The spoken language is {language}."
    encoded = base64.b64encode(bytes(audio_bytes)).decode("ascii")
    return [
        {"role": "system", "content": TRANSCRIBE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}},
            ],
        },
    ]


def clean_transcript(raw: str) -> str:
    """Drops reasoning blocks and wrapping quotes from the model reply."""
    text = THINK_BLOCK_PATTERN.sub("", str(raw or ""))
    if CLOSE_THINK_PATTERN.search(text):
        text = CLOSE_THINK_PATTERN.split(text)[-1]
    text = text.strip()
    for opening, closing in QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            return text[1:-1].strip()
    return text


def extract_reply_content(response: httpx.Response) -> str:
    """Text content of the first choice; RuntimeError when the response is malformed."""
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("llama-server returned a non-JSON transcription response") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("llama-server returned no transcription choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("llama-server transcription response is missing a message")
    return str(message.get("content") or "")


class LlmAsrEngine:
    """ASR backend that delegates transcription to an audio-capable llama-server model."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "local",
        display_name: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        clean_url = str(base_url or "").strip().rstrip("/")
        if not clean_url:
            raise ValueError("LLM base URL must not be empty")
        self.base_url = clean_url
        self.llm_model = str(model or "").strip() or "llama-server"
        self.api_key = str(api_key or "").strip() or "local"
        label = str(display_name or "").strip() or self.llm_model
        self.model_id = f"{label} (llama-server audio)"
        self.device = "llama-server"
        self.gpu_name = "via llama-server"
        self._transport = transport

    def load(self) -> None:
        """Nothing to load: the model already runs inside llama-server."""
        LOGGER.info("Using %s at %s for transcription", self.model_id, self.base_url)

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        language: str | None = None,
    ) -> TranscriptionResult:
        validate_audio_bytes(audio_bytes)
        clean_language = clean_language_hint(language)
        messages = build_transcription_messages(audio_bytes, clean_language)
        start_time = time.perf_counter()
        content = self._request_completion(messages)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        text = clean_transcript(content)
        LOGGER.info("Transcribed %d chars via llama-server in %dms", len(text), duration_ms)
        return TranscriptionResult(text=text, language=clean_language, duration_ms=duration_ms)

    def _request_completion(self, messages: list[dict[str, Any]]) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        base_payload: dict[str, Any] = {
            "model": self.llm_model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        no_think_payload = dict(base_payload, chat_template_kwargs={"enable_thinking": False})
        try:
            with httpx.Client(timeout=LLM_ASR_TIMEOUT_S, transport=self._transport) as client:
                response = client.post(url, headers=headers, json=no_think_payload)
                if response.status_code == 400:
                    response = client.post(url, headers=headers, json=base_payload)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach llama-server at {url}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:MAX_ERROR_DETAIL_CHARS]
            raise RuntimeError(f"llama-server transcription failed (HTTP {response.status_code}): {detail}")
        return extract_reply_content(response)
