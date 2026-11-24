"""Shared ASR types and audio limits used by every LocalWhisper ASR backend."""

from __future__ import annotations

from dataclasses import dataclass

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB upper bound (~13 minutes of 16 kHz mono WAV)
MIN_AUDIO_BYTES = 44  # Standard WAV header size


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    duration_ms: int


def validate_audio_bytes(audio_bytes: bytes) -> None:
    """Raises ValueError when the uploaded audio is not bytes or is outside the size limits."""
    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise ValueError("Audio payload must be bytes")
    size = len(audio_bytes)
    if size < MIN_AUDIO_BYTES:
        raise ValueError(f"Audio payload too small ({size} bytes)")
    if size > MAX_AUDIO_BYTES:
        raise ValueError(f"Audio payload exceeds {MAX_AUDIO_BYTES} byte limit")
