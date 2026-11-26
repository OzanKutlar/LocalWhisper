"""16 kHz mono 16-bit PCM microphone recorder, RMS meter, WAV encoder and silence auto-stop."""

from __future__ import annotations

from array import array
import io
import math
import struct
import threading
from typing import Callable

SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
BITS_PER_SAMPLE = 16
CHUNK_MS = 50
CHUNK_SAMPLES = SAMPLE_RATE_HZ * CHUNK_MS // 1000  # 800 samples per 50 ms chunk
CHUNK_BYTES = CHUNK_SAMPLES * 2
MAX_RECORDING_S = 120.0
MAX_CHUNKS = int(MAX_RECORDING_S * 1000 // CHUNK_MS)

DEFAULT_SPEECH_RMS = 0.018
DEFAULT_SILENCE_RMS = 0.010
DEFAULT_SILENCE_DURATION_S = 1.2
DEFAULT_INITIAL_WAIT_S = 6.0


def compute_rms_level(pcm16_bytes: bytes) -> float:
    """Computes linear normalized RMS loudness (0.0..1.0) from little-endian 16-bit mono PCM bytes."""
    sample_count = len(pcm16_bytes) // 2
    if sample_count <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm16_bytes[: sample_count * 2])
    sum_sq = 0.0
    for sample in samples:
        sum_sq += float(sample) * float(sample)
    rms = math.sqrt(sum_sq / sample_count) / 32768.0
    return min(1.0, max(0.0, rms))


def encode_pcm16_wav(pcm16_bytes: bytes, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    """Wraps raw 16-bit mono little-endian PCM bytes in a standard 44-byte RIFF/WAVE header."""
    data_len = len(pcm16_bytes)
    byte_rate = sample_rate * CHANNELS * (BITS_PER_SAMPLE // 8)
    block_align = CHANNELS * (BITS_PER_SAMPLE // 8)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        sample_rate,
        byte_rate,
        block_align,
        BITS_PER_SAMPLE,
        b"data",
        data_len,
    )
    return header + pcm16_bytes


class SilenceEndpointDetector:
    """Detects when the user has spoken and then paused for silence_duration_s."""

    def __init__(
        self,
        auto_stop: bool = True,
        silence_duration_s: float = DEFAULT_SILENCE_DURATION_S,
        initial_wait_s: float = DEFAULT_INITIAL_WAIT_S,
        max_duration_s: float = MAX_RECORDING_S,
        speech_rms: float = DEFAULT_SPEECH_RMS,
        silence_rms: float = DEFAULT_SILENCE_RMS,
        chunk_ms: int = CHUNK_MS,
    ) -> None:
        self.auto_stop = bool(auto_stop)
        self._chunk_s = max(0.01, chunk_ms / 1000.0)
        self._silence_chunks_needed = max(1, int(round(silence_duration_s / self._chunk_s)))
        self._initial_chunks_limit = max(1, int(round(initial_wait_s / self._chunk_s)))
        self._max_chunks = max(1, int(round(max_duration_s / self._chunk_s)))
        self._speech_rms = speech_rms
        self._silence_rms = silence_rms
        self.heard_speech = False
        self.total_chunks = 0
        self._quiet_chunks = 0

    def feed(self, rms_level: float) -> bool:
        """Feeds one chunk's RMS level; returns True when recording should stop automatically."""
        self.total_chunks += 1
        if self.total_chunks >= self._max_chunks:
            return True
        if not self.auto_stop:
            return False
        if rms_level >= self._speech_rms:
            self.heard_speech = True
            self._quiet_chunks = 0
            return False
        if self.heard_speech:
            if rms_level < self._silence_rms:
                self._quiet_chunks += 1
                if self._quiet_chunks >= self._silence_chunks_needed:
                    return True
            else:
                self._quiet_chunks = 0
            return False
        return self.total_chunks >= self._initial_chunks_limit


class PcMicRecorder:
    """Captures 16 kHz mono PCM from the default Windows microphone using sounddevice."""

    def __init__(
        self,
        on_amplitude: Callable[[float], None],
        on_auto_stop: Callable[[], None],
        auto_stop: bool = True,
        silence_duration_s: float = DEFAULT_SILENCE_DURATION_S,
    ) -> None:
        self._on_amplitude = on_amplitude
        self._on_auto_stop = on_auto_stop
        self._detector = SilenceEndpointDetector(auto_stop=auto_stop, silence_duration_s=silence_duration_s)
        self._buffer = io.BytesIO()
        self._lock = threading.Lock()
        self._stream: object | None = None
        self._active = False
        self._stop_signaled = False

    def start(self) -> None:
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                f"Microphone recording requires 'sounddevice' ({exc}). Run: python -m pip install -r pc_client/requirements.txt"
            ) from exc

        with self._lock:
            if self._active:
                return
            self._buffer = io.BytesIO()
            self._active = True
            self._stop_signaled = False

        def _callback(indata: bytes, _frames: int, _time_info: object, _status: object) -> None:
            raw = bytes(indata)
            with self._lock:
                if not self._active:
                    return
                self._buffer.write(raw)
            rms = compute_rms_level(raw)
            self._on_amplitude(rms)
            if not self._stop_signaled and self._detector.feed(rms):
                self._stop_signaled = True
                self._on_auto_stop()

        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE_HZ,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SAMPLES,
                callback=_callback,
            )
            stream.start()
            self._stream = stream
        except Exception as exc:
            with self._lock:
                self._active = False
            raise RuntimeError(f"Could not start PC microphone: {exc}") from exc

    def stop(self) -> bytes:
        """Stops recording and returns the complete 16 kHz mono WAV bytes (empty if no audio)."""
        stream = self._stream
        self._stream = None
        with self._lock:
            self._active = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            pcm = self._buffer.getvalue()
            self._buffer = io.BytesIO()
        if not pcm:
            return b""
        return encode_pcm16_wav(pcm)

    def cancel(self) -> None:
        self.stop()
