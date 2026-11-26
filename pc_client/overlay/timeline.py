"""Chronological thinking/tool timeline for the PC popup (Python port of AgentTimeline.kt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThinkingEntry:
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "thinking", "text": self.text}


@dataclass(frozen=True)
class ToolEntry:
    tool: str
    status: str
    summary: str
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "tool",
            "tool": self.tool,
            "status": self.status,
            "summary": self.summary,
            "local": self.local,
        }


TimelineEntry = ThinkingEntry | ToolEntry


def append_thinking(entries: list[TimelineEntry], delta: str) -> None:
    """Appends delta to the trailing ThinkingEntry, or starts a new one after a tool step."""
    if not delta:
        return
    if entries and isinstance(entries[-1], ThinkingEntry):
        entries[-1] = ThinkingEntry(entries[-1].text + delta)
    elif delta.strip():
        entries.append(ThinkingEntry(delta.lstrip()))


def reconcile_tools(entries: list[TimelineEntry], tools: list[dict[str, Any]]) -> None:
    """Updates existing server tool rows in place and appends new ones at the end."""
    server_slots = [
        idx for idx, entry in enumerate(entries) if isinstance(entry, ToolEntry) and not entry.local
    ]
    for pos, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        updated = ToolEntry(
            tool=str(tool.get("tool", "")),
            status=str(tool.get("status", "ok")),
            summary=str(tool.get("summary", tool.get("tool", "tool"))),
            local=False,
        )
        if pos < len(server_slots):
            entries[server_slots[pos]] = updated
        else:
            entries.append(updated)


def from_legacy(thinking: str, tool_calls: list[dict[str, Any]]) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    if thinking and thinking.strip():
        entries.append(ThinkingEntry(thinking.strip()))
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        entries.append(
            ToolEntry(
                tool=str(call.get("tool", "")),
                status=str(call.get("status", "ok")),
                summary=str(call.get("summary", call.get("tool", "tool"))),
            )
        )
    return entries


def thinking_text(entries: list[TimelineEntry]) -> str:
    parts = [e.text.strip() for e in entries if isinstance(e, ThinkingEntry) and e.text.strip()]
    return "\n\n".join(parts)


def tool_calls(entries: list[TimelineEntry]) -> list[dict[str, str]]:
    return [
        {"tool": e.tool, "status": e.status, "summary": e.summary}
        for e in entries
        if isinstance(e, ToolEntry)
    ]


def tool_count(entries: list[TimelineEntry]) -> int:
    return sum(1 for e in entries if isinstance(e, ToolEntry))


def has_thinking(entries: list[TimelineEntry]) -> bool:
    return any(isinstance(e, ThinkingEntry) and bool(e.text.strip()) for e in entries)


def serialize_timeline(entries: list[TimelineEntry]) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in entries]


def parse_timeline(raw: Any) -> list[TimelineEntry]:
    if not isinstance(raw, list):
        return []
    entries: list[TimelineEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "thinking":
            entries.append(ThinkingEntry(str(item.get("text", ""))))
        elif kind == "tool":
            entries.append(
                ToolEntry(
                    tool=str(item.get("tool", "")),
                    status=str(item.get("status", "ok")),
                    summary=str(item.get("summary", "")),
                    local=bool(item.get("local", False)),
                )
            )
    return entries
