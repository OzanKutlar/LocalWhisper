"""Asks a running llama-server which input modalities (vision, audio) its loaded model supports.

llama-server answers GET /props (at the server root, not under /v1) with a JSON object that
contains \"modalities\": {\"vision\": bool, \"audio\": bool} on builds with multimodal support.
The user is expected to start llama-server before LocalWhisper, so the probe is short.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger("localwhisper.llm_caps")

PROBE_TIMEOUT_S = 3.0
PROBE_ATTEMPTS = 2
MAX_PROBE_ATTEMPTS = 5
PROBE_RETRY_DELAY_S = 0.5
MAX_DETAIL_CHARS = 200
MAX_MODEL_NAME_CHARS = 120
WINDOWS_PATH_SEP = chr(92)  # backslash, normalized to '/' before taking the file name


@dataclass(frozen=True)
class LlmCapabilities:
    """What the llama-server reported about its loaded model."""

    reachable: bool
    vision: bool = False
    audio: bool = False
    model_name: str = ""
    detail: str = ""

    def modalities(self) -> dict[str, bool]:
        return {"vision": self.vision, "audio": self.audio}


def server_root_from_base_url(base_url: str) -> str:
    """'http://host:port/v1' -> 'http://host:port' (where llama-server serves /props)."""
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        raise ValueError("LLM base URL must not be empty")
    if clean.lower().endswith("/v1"):
        clean = clean[: -len("/v1")].rstrip("/")
    return clean


def parse_props(payload: Any) -> LlmCapabilities:
    """Reads the modalities from a /props JSON payload. Missing fields mean 'not supported'."""
    if not isinstance(payload, dict):
        return LlmCapabilities(reachable=True, detail="/props did not return a JSON object")
    model_name = _model_name_from_props(payload)
    modalities = payload.get("modalities")
    if not isinstance(modalities, dict):
        return LlmCapabilities(
            reachable=True,
            model_name=model_name,
            detail="/props has no 'modalities' field (older llama-server build?)",
        )
    return LlmCapabilities(
        reachable=True,
        vision=modalities.get("vision") is True,
        audio=modalities.get("audio") is True,
        model_name=model_name,
        detail="ok",
    )


def _model_name_from_props(payload: dict[str, Any]) -> str:
    raw = payload.get("model_alias") or payload.get("model_path") or ""
    normalized = str(raw).replace(WINDOWS_PATH_SEP, "/")
    return os.path.basename(normalized).strip()[:MAX_MODEL_NAME_CHARS]


def probe_llm_capabilities(
    base_url: str,
    api_key: str = "local",
    attempts: int = PROBE_ATTEMPTS,
    delay_s: float = PROBE_RETRY_DELAY_S,
    transport: httpx.BaseTransport | None = None,
) -> LlmCapabilities:
    """Queries /props with a small, bounded retry budget. Never raises; unreachable -> reachable=False."""
    try:
        url = f"{server_root_from_base_url(base_url)}/props"
    except ValueError as exc:
        return LlmCapabilities(reachable=False, detail=str(exc))
    headers = {"Authorization": f"Bearer {str(api_key or '').strip() or 'local'}"}
    bounded_attempts = max(1, min(int(attempts), MAX_PROBE_ATTEMPTS))
    last = LlmCapabilities(reachable=False, detail="not attempted")
    with httpx.Client(timeout=PROBE_TIMEOUT_S, transport=transport) as client:
        for attempt in range(1, bounded_attempts + 1):
            last, should_retry = _probe_once(client, url, headers)
            if not should_retry:
                return last
            LOGGER.info("llama-server probe attempt %d/%d: %s", attempt, bounded_attempts, last.detail)
            if attempt < bounded_attempts and delay_s > 0:
                time.sleep(delay_s)
    return last


def _probe_once(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
) -> tuple[LlmCapabilities, bool]:
    """One /props request; the bool says whether retrying could help."""
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        detail = f"could not reach {url}: {exc}"[:MAX_DETAIL_CHARS]
        return LlmCapabilities(reachable=False, detail=detail), True
    if response.status_code == 503:
        return LlmCapabilities(reachable=False, detail="llama-server is still loading its model (HTTP 503)"), True
    if response.status_code >= 400:
        detail = f"{url} returned HTTP {response.status_code}"
        return LlmCapabilities(reachable=True, detail=detail), False
    try:
        payload = response.json()
    except ValueError:
        return LlmCapabilities(reachable=True, detail="/props did not return JSON"), False
    return parse_props(payload), False


def choose_asr_backend(requested: str, caps: LlmCapabilities | None) -> str:
    """Returns 'local' or 'llm'. Raises RuntimeError when 'llm' is forced but audio is unsupported."""
    clean = str(requested or "").strip().lower()
    if clean == "local":
        return "local"
    if caps is not None and caps.reachable and caps.audio:
        return "llm"
    if clean == "llm":
        detail = caps.detail if caps is not None else "not probed"
        raise RuntimeError(
            f"--asr-backend llm was requested but llama-server does not report audio input ({detail})"
        )
    return "local"
