"""CUDA-backed Qwen3-ASR engine for LocalWhisper server."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass

import torch
from qwen_asr import Qwen3ASRModel

LOGGER = logging.getLogger("localwhisper.asr")
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB upper bound (~13 minutes of 16 kHz mono WAV)
MIN_AUDIO_BYTES = 44  # Standard WAV header size


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    duration_ms: int


class AsrEngine:
    """Loads and executes Qwen3-ASR strictly on a CUDA device."""

    def __init__(self, model_id: str, device: str = "cuda:0") -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not device.startswith("cuda"):
            raise ValueError(f"LocalWhisper requires a CUDA device, got: {device}")

        self.model_id = model_id
        self.device = device
        self.gpu_name = "unknown"
        self._model: Qwen3ASRModel | None = None
        self._inference_lock = threading.Lock()

    def load(self) -> None:
        """Verifies CUDA availability and loads the Qwen3-ASR model onto GPU."""
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is not available in this Python environment. "
                "Install PyTorch with CUDA support for your RTX 5060 Ti."
            )

        device_index = self._parse_device_index(self.device)
        if device_index >= torch.cuda.device_count():
            raise RuntimeError(
                f"Requested device '{self.device}' but only "
                f"{torch.cuda.device_count()} CUDA device(s) detected."
            )

        self.gpu_name = torch.cuda.get_device_name(device_index)
        LOGGER.info("Loading %s on %s (%s) in bfloat16...", self.model_id, self.device, self.gpu_name)

        start_time = time.perf_counter()
        self._model = Qwen3ASRModel.from_pretrained(
            self.model_id,
            dtype=torch.bfloat16,
            device_map=self.device,
        )
        elapsed_s = time.perf_counter() - start_time
        LOGGER.info("Model %s loaded on %s in %.2fs", self.model_id, self.gpu_name, elapsed_s)

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribes uploaded WAV audio bytes synchronously on CUDA."""
        if self._model is None:
            raise RuntimeError("ASR model has not been loaded yet")
        self._validate_audio_bytes(audio_bytes)

        normalized_language = language.strip() if language else None
        temp_path = self._write_temp_wav(audio_bytes)
        try:
            return self._run_inference(temp_path, normalized_language)
        finally:
            self._cleanup_file(temp_path)

    def _run_inference(self, wav_path: str, language: str | None) -> TranscriptionResult:
        assert self._model is not None
        start_time = time.perf_counter()
        with self._inference_lock:
            results = self._model.transcribe(audio=wav_path, language=language)
        duration_ms = int((time.perf_counter() - start_time) * 1000)

        text, detected_language = self._extract_result(results)
        LOGGER.info(
            "Transcribed %d chars (lang=%s) in %dms on %s",
            len(text),
            detected_language or "auto",
            duration_ms,
            self.device,
        )
        return TranscriptionResult(
            text=text,
            language=detected_language,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _extract_result(results: object) -> tuple[str, str]:
        if not results:
            return "", ""
        first = results[0] if isinstance(results, (list, tuple)) else results
        text = str(getattr(first, "text", first)).strip()
        lang = str(getattr(first, "language", "") or "").strip()
        return text, lang

    @staticmethod
    def _validate_audio_bytes(audio_bytes: bytes) -> None:
        size = len(audio_bytes)
        if size < MIN_AUDIO_BYTES:
            raise ValueError(f"Audio payload too small ({size} bytes)")
        if size > MAX_AUDIO_BYTES:
            raise ValueError(f"Audio payload exceeds {MAX_AUDIO_BYTES} byte limit")

    @staticmethod
    def _write_temp_wav(audio_bytes: bytes) -> str:
        fd, temp_path = tempfile.mkstemp(prefix="localwhisper_", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(audio_bytes)
        except Exception:
            AsrEngine._cleanup_file(temp_path)
            raise
        return temp_path

    @staticmethod
    def _cleanup_file(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            LOGGER.warning("Failed to delete temp audio file %s: %s", path, exc)

    @staticmethod
    def _parse_device_index(device: str) -> int:
        if ":" not in device:
            return 0
        try:
            index = int(device.split(":", 1)[1])
        except ValueError as exc:
            raise ValueError(f"Invalid CUDA device specification: {device}") from exc
        if index < 0:
            raise ValueError(f"CUDA device index must be non-negative: {device}")
        return index
