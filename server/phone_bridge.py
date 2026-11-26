"""Runs phone commands over the phone link for the agent.

Every helper returns a result dict instead of raising, so an offline phone becomes a clear tool
result ("phone_offline") that the model can explain to the user.
"""

from __future__ import annotations

import logging
from typing import Any

from phone_hub import NoPhoneConnectedError, PhoneDispatchError, PhoneHub

LOGGER = logging.getLogger("localwhisper.phone_bridge")

ORIGIN_PHONE = "phone"
ORIGIN_PC_OVERLAY = "pc_overlay"
KNOWN_ORIGINS = frozenset({ORIGIN_PHONE, ORIGIN_PC_OVERLAY})

CMD_SNAPSHOT = "phone.snapshot"
CMD_UI_ACTION = "phone.ui_action"
CMD_TARGET_PREVIEW = "phone.target_preview"
CMD_ANDROID_INTENT = "android.intent"
CMD_CLIPBOARD_READ = "clipboard.read"
CMD_CLIPBOARD_WRITE = "clipboard.write"

SNAPSHOT_TIMEOUT_S = 10.0
UI_ACTION_TIMEOUT_S = 35.0
PREVIEW_TIMEOUT_S = 10.0
INTENT_TIMEOUT_S = 15.0
MAX_PREVIEW_B64_CHARS = 600_000

REASON_OFFLINE = "phone_offline"
REASON_ERROR = "phone_error"
REASON_NO_SNAPSHOT = "no_phone_snapshot"


def normalize_origin(raw: Any) -> str:
    """'phone' or 'pc_overlay'; anything unknown counts as the phone (the original behaviour)."""
    clean = str(raw or "").strip().lower()
    return clean if clean in KNOWN_ORIGINS else ORIGIN_PHONE


def failure(reason: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "error": message}


class PhoneBridge:
    """Phone commands used by AgentEngine when a turn comes from the PC popup."""

    def __init__(self, hub: PhoneHub | None) -> None:
        self._hub = hub

    async def snapshot(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """(fresh phone screen snapshot, ok result), or (None, failure) when the phone can't answer."""
        result = await self._call(CMD_SNAPSHOT, {}, SNAPSHOT_TIMEOUT_S)
        if result.get("ok") is False:
            return None, result
        screen = result.get("phone_screen")
        if not isinstance(screen, dict) or not screen:
            return None, failure(REASON_NO_SNAPSHOT, "The phone did not return a screen snapshot.")
        return screen, {"ok": True}

    async def ui_action(self, action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Runs an approved tap/type/scroll/screenshot; returns (action result, fresh snapshot or None)."""
        result = await self._call(CMD_UI_ACTION, {"action": dict(action)}, UI_ACTION_TIMEOUT_S)
        action_result = result.get("action_result")
        if not isinstance(action_result, dict):
            if result.get("ok") is False:
                return result, None
            return failure(REASON_ERROR, "The phone did not report the result of the action."), None
        screen = result.get("phone_screen")
        return action_result, (screen if isinstance(screen, dict) and screen else None)

    async def target_preview(self, action: dict[str, Any]) -> str:
        """Zoomed JPEG (base64) of the tap target for the approval card; '' when unavailable."""
        result = await self._call(CMD_TARGET_PREVIEW, {"action": dict(action)}, PREVIEW_TIMEOUT_S)
        preview = str(result.get("image_jpeg_b64", "") or "")
        if not preview or len(preview) > MAX_PREVIEW_B64_CHARS:
            return ""
        return preview

    async def android_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Alarm, timer, URL, web search or app launch on the phone."""
        return await self._call(CMD_ANDROID_INTENT, {"action": dict(payload)}, INTENT_TIMEOUT_S)

    async def read_clipboard(self) -> dict[str, Any]:
        """Reads text from the phone clipboard over the phone link."""
        return await self._call(CMD_CLIPBOARD_READ, {}, SNAPSHOT_TIMEOUT_S)

    async def write_clipboard(self, text: str) -> dict[str, Any]:
        """Writes text to the phone clipboard over the phone link."""
        return await self._call(CMD_CLIPBOARD_WRITE, {"text": str(text)}, SNAPSHOT_TIMEOUT_S)

    async def _call(self, command: str, arguments: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if self._hub is None:
            return failure(REASON_OFFLINE, "This server has no phone link.")
        try:
            _name, result = await self._hub.call(command, arguments, timeout_s)
        except NoPhoneConnectedError as exc:
            return failure(REASON_OFFLINE, str(exc))
        except (PhoneDispatchError, ValueError) as exc:
            LOGGER.warning("Phone command '%s' failed: %s", command, exc)
            return failure(REASON_ERROR, str(exc))
        if not isinstance(result, dict):
            return failure(REASON_ERROR, "The phone sent an invalid result.")
        return result
