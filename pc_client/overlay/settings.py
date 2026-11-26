"""User settings for the PC Jarvis popup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from overlay.session_store import default_storage_dir
from overlay.style import clamp_ui_scale


@dataclass
class OverlaySettings:
    hotkey: str = "Ctrl+Alt+Space"
    start_in_keyboard_mode: bool = False
    start_in_agent_mode: bool = True
    auto_stop_on_silence: bool = True
    silence_duration_s: float = 1.2
    agent_resume_timeout_s: int = 15
    language: str = ""
    selected_target_id: str = ""
    ui_scale: float = 1.2

    @staticmethod
    def load(path: Path | None = None) -> OverlaySettings:
        file_path = path or (default_storage_dir() / "overlay_settings.json")
        if not file_path.is_file():
            return OverlaySettings()
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return OverlaySettings()
        if not isinstance(raw, dict):
            return OverlaySettings()
        return OverlaySettings(
            hotkey=str(raw.get("hotkey", "Ctrl+Alt+Space") or "Ctrl+Alt+Space").strip(),
            start_in_keyboard_mode=bool(raw.get("start_in_keyboard_mode", False)),
            start_in_agent_mode=bool(raw.get("start_in_agent_mode", True)),
            auto_stop_on_silence=bool(raw.get("auto_stop_on_silence", True)),
            silence_duration_s=max(0.5, min(float(raw.get("silence_duration_s", 1.2) or 1.2), 5.0)),
            agent_resume_timeout_s=max(0, min(int(raw.get("agent_resume_timeout_s", 15) or 0), 60)),
            language=str(raw.get("language", "") or "").strip(),
            selected_target_id=str(raw.get("selected_target_id", "") or "").strip(),
            ui_scale=clamp_ui_scale(raw.get("ui_scale", 1.2)),
        )

    def save(self, path: Path | None = None) -> None:
        file_path = path or (default_storage_dir() / "overlay_settings.json")
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = file_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, file_path)
        except OSError:
            pass
