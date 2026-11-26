"""Cross-device workspace scenes: named, ordered PC and phone steps run with one command.

Scenes live in server/scenes.json. A scene that contains any step that would normally ask for
approval (locking the PC, power actions, closing a window) is approved once as a whole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("localwhisper.scenes")

MAX_SCENES = 30
MAX_STEPS_PER_SCENE = 20
MAX_NAME_CHARS = 40
MAX_DESCRIPTION_CHARS = 200
MAX_STEP_ARGUMENT_CHARS = 4000
MAX_WAIT_MS = 5000
MAX_LAYOUT_WINDOWS = 12
MAX_LAYOUT_STEPS = 2
MAX_ERROR_CHARS = 200
WAIT_TOOL = "wait"
FILE_VERSION = 1

ALLOWED_SCENE_TOOLS = frozenset(
    {
        WAIT_TOOL,
        "execute_pc_ahk_sequence",
        "manage_pc_app",
        "control_pc_media",
        "open_pc_url",
        "control_pc_audio",
        "control_pc_display",
        "control_pc_power",
        "arrange_pc_windows",
        "write_clipboard",
        "trigger_android_intent",
        "control_phone",
    }
)

StepExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
StepSummarizer = Callable[[str, dict[str, Any]], str]


@dataclass(frozen=True)
class SceneStep:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class Scene:
    name: str
    description: str
    steps: tuple[SceneStep, ...]
    continue_on_error: bool = True

    @property
    def key(self) -> str:
        return scene_key(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "continue_on_error": self.continue_on_error,
            "steps": [step.to_dict() for step in self.steps],
        }


def scene_key(name: Any) -> str:
    return " ".join(str(name or "").split()).lower()


def clean_scene_name(raw: Any) -> str:
    clean = " ".join(str(raw or "").split())
    if not clean:
        raise ValueError("Scene name must not be empty")
    if len(clean) > MAX_NAME_CHARS:
        raise ValueError(f"Scene name must be at most {MAX_NAME_CHARS} characters")
    return clean


def clamp_wait_ms(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(value, MAX_WAIT_MS))


def parse_scene_step(raw: Any) -> SceneStep:
    """One step: {"tool": ..., "arguments": {...}} or {"wait_ms": n}. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("Each scene step must be an object")
    tool = str(raw.get("tool", "") or "").strip()
    if not tool and raw.get("wait_ms") is not None:
        tool = WAIT_TOOL
    if tool not in ALLOWED_SCENE_TOOLS:
        raise ValueError(
            f"Tool '{tool}' cannot be used in a scene; allowed: {', '.join(sorted(ALLOWED_SCENE_TOOLS))}"
        )
    raw_args = raw.get("arguments")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ValueError(f"Arguments for '{tool}' must be an object")
    if tool == WAIT_TOOL:
        return SceneStep(WAIT_TOOL, {"ms": clamp_wait_ms(raw.get("wait_ms", raw_args.get("ms")))})
    try:
        encoded = json.dumps(raw_args)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Arguments for '{tool}' must be plain JSON") from exc
    if len(encoded) > MAX_STEP_ARGUMENT_CHARS:
        raise ValueError(f"Arguments for '{tool}' exceed {MAX_STEP_ARGUMENT_CHARS} characters")
    return SceneStep(tool, json.loads(encoded))


def parse_scene_steps(raw: Any) -> list[SceneStep]:
    """Parses a list of steps; a tool step with wait_ms gets a wait step right after it."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("steps must be a list")
    if len(raw) > MAX_STEPS_PER_SCENE:
        raise ValueError(f"A scene can have at most {MAX_STEPS_PER_SCENE} steps")
    steps: list[SceneStep] = []
    for item in raw:
        step = parse_scene_step(item)
        steps.append(step)
        trailing_wait = clamp_wait_ms(item.get("wait_ms")) if step.tool != WAIT_TOOL else 0
        if trailing_wait > 0:
            steps.append(SceneStep(WAIT_TOOL, {"ms": trailing_wait}))
    if len(steps) > MAX_STEPS_PER_SCENE:
        raise ValueError(f"A scene can have at most {MAX_STEPS_PER_SCENE} steps (waits count as steps)")
    return steps


def build_scene(name: Any, description: Any, steps: list[SceneStep], continue_on_error: bool = True) -> Scene:
    clean_name = clean_scene_name(name)
    if not steps:
        raise ValueError("A scene needs at least one step")
    if len(steps) > MAX_STEPS_PER_SCENE:
        raise ValueError(f"A scene can have at most {MAX_STEPS_PER_SCENE} steps")
    if all(step.tool == WAIT_TOOL for step in steps):
        raise ValueError("A scene needs at least one step that is not a wait")
    for step in steps:
        if step.tool not in ALLOWED_SCENE_TOOLS:
            raise ValueError(f"Tool '{step.tool}' cannot be used in a scene")
    clean_desc = " ".join(str(description or "").split())[:MAX_DESCRIPTION_CHARS]
    return Scene(clean_name, clean_desc, tuple(steps), bool(continue_on_error))


def _window_target(win: dict[str, Any]) -> str:
    process = str(win.get("process_name", "") or "").strip()
    if process.lower().endswith(".exe"):
        process = process[:-4]
    if process and process.lower() != "unknown":
        return process
    return " ".join(str(win.get("title", "") or "").split())[:60]


def _window_monitor(win: dict[str, Any]) -> str:
    try:
        return str(max(1, int(win.get("monitor") or 1)))
    except (TypeError, ValueError):
        return "1"


def _arrangement_for(win: dict[str, Any], target: str) -> dict[str, Any] | None:
    monitor = _window_monitor(win)
    if win.get("state") == "maximized":
        return {"target": target, "monitor": monitor, "slot": "maximize"}
    bounds = win.get("bounds")
    if not isinstance(bounds, dict):
        return None
    try:
        x, y, width, height = (int(bounds[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 1 or height <= 1:
        return None
    return {
        "target": target,
        "monitor": monitor,
        "slot": "custom",
        "rect": {"x": x, "y": y, "width": width, "height": height},
    }


def layout_to_arrange_steps(layout: Any) -> list[SceneStep]:
    """Turns a get_pc_layout result into arrange_pc_windows steps (one window per app)."""
    windows = layout.get("windows") if isinstance(layout, dict) else None
    if not isinstance(windows, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_items = MAX_LAYOUT_WINDOWS * MAX_LAYOUT_STEPS
    for win in windows:
        if len(items) >= max_items:
            break
        if not isinstance(win, dict) or win.get("state") == "minimized":
            continue
        target = _window_target(win)
        if not target or target.lower() in seen:
            continue
        item = _arrangement_for(win, target)
        if item is None:
            continue
        seen.add(target.lower())
        items.append(item)
    return [
        SceneStep("arrange_pc_windows", {"arrangements": items[start : start + MAX_LAYOUT_WINDOWS]})
        for start in range(0, len(items), MAX_LAYOUT_WINDOWS)
    ]


STARTER_SCENES: tuple[Scene, ...] = (
    Scene(
        name="Start Work",
        description="PC volume to 40% and phone Do Not Disturb on. Add your apps and layout by voice.",
        steps=(
            SceneStep("control_pc_audio", {"action": "set_volume", "volume_pct": 40}),
            SceneStep("control_phone", {"action": "set_dnd", "enabled": True}),
        ),
    ),
    Scene(
        name="Meeting Mode",
        description="Unmute the PC mic, volume to 50%, phone on vibrate with Do Not Disturb.",
        steps=(
            SceneStep("control_pc_audio", {"action": "unmute_mic"}),
            SceneStep("control_pc_audio", {"action": "set_volume", "volume_pct": 50}),
            SceneStep("control_phone", {"action": "set_dnd", "enabled": True}),
            SceneStep("control_phone", {"action": "set_ringer", "mode": "vibrate"}),
        ),
    ),
    Scene(
        name="End of Day",
        description="Phone Do Not Disturb off, ringer back to normal, then lock the PC.",
        steps=(
            SceneStep("control_phone", {"action": "set_dnd", "enabled": False}),
            SceneStep("control_phone", {"action": "set_ringer", "mode": "normal"}),
            SceneStep("manage_pc_app", {"action": "lock_pc"}),
        ),
    ),
)


def _scene_from_dict(raw: Any) -> Scene | None:
    if not isinstance(raw, dict):
        return None
    try:
        steps = parse_scene_steps(raw.get("steps"))
        return build_scene(
            raw.get("name"),
            raw.get("description", ""),
            steps,
            bool(raw.get("continue_on_error", True)),
        )
    except ValueError as exc:
        LOGGER.warning("Skipping invalid saved scene %r: %s", raw.get("name"), exc)
        return None


class SceneStore:
    """Thread-safe scene storage backed by a JSON file. Seeds starter scenes on first run."""

    def __init__(self, path: Path, seed_defaults: bool = True) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._scenes: dict[str, Scene] = {}
        file_existed = self._load()
        if not file_existed and seed_defaults:
            with self._lock:
                for scene in STARTER_SCENES:
                    self._scenes[scene.key] = scene
                self._save_locked()

    def list(self) -> list[Scene]:
        with self._lock:
            return sorted(self._scenes.values(), key=lambda scene: scene.key)

    def names(self) -> list[str]:
        return [scene.name for scene in self.list()]

    def get(self, name: Any) -> Scene | None:
        with self._lock:
            return self._scenes.get(scene_key(name))

    def save(
        self,
        name: Any,
        description: Any,
        steps: list[SceneStep],
        continue_on_error: bool = True,
    ) -> Scene:
        """Creates or replaces a scene. Raises ValueError for invalid scenes or a full store."""
        scene = build_scene(name, description, steps, continue_on_error)
        with self._lock:
            if scene.key not in self._scenes and len(self._scenes) >= MAX_SCENES:
                raise ValueError(f"At most {MAX_SCENES} scenes can be saved; delete one first")
            self._scenes[scene.key] = scene
            self._save_locked()
        return scene

    def delete(self, name: Any) -> bool:
        with self._lock:
            removed = self._scenes.pop(scene_key(name), None)
            if removed is not None:
                self._save_locked()
            return removed is not None

    def _load(self) -> bool:
        """Loads saved scenes; True when the file existed (so starter scenes are not re-added)."""
        if not self.path.is_file():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not read scenes from %s: %s", self.path, exc)
            return True
        raw_scenes = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(raw_scenes, list):
            return True
        with self._lock:
            for raw in raw_scenes[:MAX_SCENES]:
                scene = _scene_from_dict(raw)
                if scene is not None:
                    self._scenes[scene.key] = scene
        return True

    def _save_locked(self) -> None:
        payload = {
            "version": FILE_VERSION,
            "scenes": [scene.to_dict() for scene in sorted(self._scenes.values(), key=lambda s: s.key)],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as exc:
            LOGGER.warning("Could not save scenes to %s: %s", self.path, exc)


def _step_error(output: Any) -> str:
    if not isinstance(output, dict):
        return "invalid step result"
    for source in (output, output.get("result")):
        if isinstance(source, dict):
            error = source.get("error") or source.get("message")
            if error:
                return str(error)[:MAX_ERROR_CHARS]
    return "step failed"


class SceneRunner:
    """Runs a scene's steps in order through an executor and reports a result per step."""

    def __init__(
        self,
        executor: StepExecutor,
        summarize: StepSummarizer | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._executor = executor
        self._summarize = summarize or (lambda tool, _arguments: tool)
        self._sleep = sleep

    def describe(self, step: SceneStep) -> str:
        if step.tool == WAIT_TOOL:
            return f"Wait {clamp_wait_ms(step.arguments.get('ms'))} ms"
        try:
            return self._summarize(step.tool, step.arguments)
        except Exception:  # noqa: BLE001 - a summary must never stop a scene
            return step.tool

    async def run(self, scene: Scene) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        ok_count = 0
        failed = 0
        stopped = False
        for index, step in enumerate(scene.steps[:MAX_STEPS_PER_SCENE], start=1):
            ok, error = await self._run_step(step)
            entry: dict[str, Any] = {
                "index": index,
                "tool": step.tool,
                "summary": self.describe(step),
                "ok": ok,
            }
            if error:
                entry["error"] = error
            results.append(entry)
            if ok:
                ok_count += 1
                continue
            failed += 1
            if not scene.continue_on_error:
                stopped = True
                break
        return {
            "ok": failed == 0 and not stopped,
            "scene": scene.name,
            "steps_run": len(results),
            "steps_ok": ok_count,
            "steps_total": len(scene.steps),
            "stopped_early": stopped,
            "results": results,
        }

    async def _run_step(self, step: SceneStep) -> tuple[bool, str]:
        if step.tool == WAIT_TOOL:
            wait_ms = clamp_wait_ms(step.arguments.get("ms"))
            if wait_ms > 0:
                await self._sleep(wait_ms / 1000.0)
            return True, ""
        try:
            output = await self._executor(step.tool, dict(step.arguments))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failing step is reported, not raised
            LOGGER.warning("Scene step '%s' failed: %s", step.tool, exc)
            return False, str(exc)[:MAX_ERROR_CHARS]
        ok = isinstance(output, dict) and output.get("ok") is not False
        return ok, ("" if ok else _step_error(output))
