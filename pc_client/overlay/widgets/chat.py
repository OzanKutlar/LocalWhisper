"""Chat bubbles and the collapsible thinking/tool timeline for the PC Jarvis popup."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from overlay.animations import PulseDot, TypingDots
from overlay.style import px
from overlay.theme import OverlayPalette
from overlay.timeline import ThinkingEntry, TimelineEntry, ToolEntry, tool_count

KIND_THINKING = "thinking"
KIND_TOOL = "tool"


def _tool_prefix(status: str) -> str:
    if status in ("ok", "queued_on_phone"):
        return "✓ "
    if status == "running":
        return "⟳ "
    if status == "pending_confirmation":
        return "⏳ "
    if status in ("rejected", "error"):
        return "✕ "
    if status in ("no_change", "skipped"):
        return "≈ "
    return "• "


def _tool_color(status: str, palette: OverlayPalette) -> str:
    if status in ("ok", "queued_on_phone"):
        return palette.success
    if status in ("running", "pending_confirmation"):
        return palette.accent
    if status in ("rejected", "error"):
        return palette.error
    if status in ("no_change", "skipped"):
        return palette.text_secondary
    return palette.text_primary


def _plain_label(parent: QWidget, selectable: bool = True) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _ClickableRow(QWidget):
    def __init__(self, on_click: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(event)


class TimelineCardWidget(QFrame):
    """Collapsible card with the agent's thinking and tool steps, updated in place while streaming."""

    def __init__(
        self,
        palette: OverlayPalette,
        start_expanded: bool = True,
        live: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._expanded = bool(start_expanded)
        self._live = bool(live)
        self._entries: list[TimelineEntry] = []
        self._rows: list[tuple[str, QLabel]] = []
        self.setObjectName("timelineCard")
        self.setStyleSheet(
            f"QFrame#timelineCard {{ background-color: {palette.card_elevated}; "
            f"border: 1px solid {palette.divider}; border-radius: {px(14)}px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(14), px(10), px(14), px(12))
        layout.setSpacing(px(8))

        header = _ClickableRow(self._toggle, self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(px(8))
        self._dot = PulseDot(palette.accent, px(4), header)
        self._title = QLabel("Thinking…", header)
        self._title.setStyleSheet(
            f"QLabel {{ color: {palette.accent}; font-size: {px(13.5)}px; font-weight: 700; background: transparent; }}"
        )
        self._count = QLabel("", header)
        self._count.setStyleSheet(
            f"QLabel {{ color: {palette.text_secondary}; font-size: {px(12.5)}px; background: transparent; }}"
        )
        self._chevron = QLabel("▾", header)
        self._chevron.setStyleSheet(
            f"QLabel {{ color: {palette.text_secondary}; font-size: {px(14)}px; background: transparent; }}"
        )
        header_layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self._title)
        header_layout.addWidget(self._count)
        header_layout.addStretch(1)
        header_layout.addWidget(self._chevron)
        layout.addWidget(header)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(px(8))
        self._body.setVisible(self._expanded)
        layout.addWidget(self._body)
        self.set_live(self._live)

    def set_live(self, live: bool) -> None:
        self._live = bool(live)
        self._dot.setVisible(self._live)
        self._dot.set_pulsing(self._live)
        self._update_header()
        self.setVisible(bool(self._entries) or self._live)

    def sync_entries(self, entries: list[TimelineEntry]) -> None:
        self._entries = list(entries)
        self.setVisible(bool(self._entries) or self._live)
        self._update_header()
        if self._expanded:
            self._sync_rows()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._update_header()
        if self._expanded:
            self._sync_rows()

    def _update_header(self) -> None:
        count = tool_count(self._entries)
        self._title.setText("Thinking…" if self._live else "Thought process")
        self._count.setText(f"· {count} tool step{'' if count == 1 else 's'}" if count else "")
        self._chevron.setText("▾" if self._expanded else "▸")

    def _sync_rows(self) -> None:
        for index, entry in enumerate(self._entries):
            kind = KIND_THINKING if isinstance(entry, ThinkingEntry) else KIND_TOOL
            if index < len(self._rows) and self._rows[index][0] != kind:
                self._truncate_rows(index)
            if index < len(self._rows):
                self._bind_row(self._rows[index][1], entry)
                continue
            label = _plain_label(self._body)
            self._bind_row(label, entry)
            self._rows.append((kind, label))
            self._body_layout.addWidget(label)
        self._truncate_rows(len(self._entries))

    def _truncate_rows(self, keep: int) -> None:
        while len(self._rows) > keep:
            _kind, label = self._rows.pop()
            self._body_layout.removeWidget(label)
            label.deleteLater()

    def _bind_row(self, label: QLabel, entry: TimelineEntry) -> None:
        p = self._palette
        if isinstance(entry, ThinkingEntry):
            text = entry.text.strip()
            style = (
                f"QLabel {{ color: {p.text_secondary}; font-size: {px(13.5)}px; font-style: italic; "
                f"background: transparent; border: none; border-left: {px(3)}px solid {p.accent}; "
                f"padding: {px(2)}px 0px {px(2)}px {px(10)}px; }}"
            )
        elif isinstance(entry, ToolEntry):
            text = _tool_prefix(entry.status) + entry.summary
            style = (
                f"QLabel {{ color: {_tool_color(entry.status, p)}; background-color: {p.pill_bg}; border: none; "
                f"border-radius: {px(10)}px; padding: {px(5)}px {px(11)}px; font-size: {px(13)}px; font-weight: 600; }}"
            )
        else:
            return
        if label.text() != text:
            label.setText(text)
        if label.styleSheet() != style:
            label.setStyleSheet(style)


class TurnWidget(QWidget):
    """One user prompt, its thinking timeline and the assistant reply."""

    def __init__(
        self,
        palette: OverlayPalette,
        user_prompt: str,
        initial_reply: str,
        expand_timeline: bool = False,
        is_placeholder_reply: bool = False,
        live: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(8))

        if user_prompt.strip():
            layout.addWidget(self._build_user_bubble(user_prompt))

        self.timeline_card = TimelineCardWidget(
            palette,
            start_expanded=expand_timeline or live,
            live=live,
            parent=self,
        )
        layout.addWidget(self.timeline_card)

        reply_frame = QFrame(self)
        reply_frame.setObjectName("replyBubble")
        reply_frame.setStyleSheet(
            f"QFrame#replyBubble {{ background-color: {palette.card_elevated}; border: none; border-radius: {px(16)}px; }}"
        )
        reply_layout = QHBoxLayout(reply_frame)
        reply_layout.setContentsMargins(px(16), px(12), px(16), px(12))
        reply_layout.setSpacing(px(10))
        self._dots = TypingDots(palette.accent, px(4), reply_frame)
        self.reply_label = _plain_label(reply_frame)
        reply_layout.addWidget(self._dots, 0, Qt.AlignmentFlag.AlignVCenter)
        reply_layout.addWidget(self.reply_label, 1)
        layout.addWidget(reply_frame)
        self.set_reply(initial_reply, is_placeholder=is_placeholder_reply)

    def _build_user_bubble(self, user_prompt: str) -> QFrame:
        p = self._palette
        bubble = QFrame(self)
        bubble.setObjectName("userBubble")
        bubble.setStyleSheet(
            f"QFrame#userBubble {{ background-color: {p.accent_container}; border: none; border-radius: {px(16)}px; }}"
        )
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(px(14), px(9), px(14), px(10))
        bubble_layout.setSpacing(px(2))
        you = QLabel("You", bubble)
        you.setStyleSheet(
            f"QLabel {{ color: {p.accent}; font-size: {px(11.5)}px; font-weight: 700; background: transparent; }}"
        )
        prompt = _plain_label(bubble)
        prompt.setText(user_prompt)
        prompt.setStyleSheet(
            f"QLabel {{ color: {p.text_primary}; font-size: {px(15)}px; background: transparent; }}"
        )
        bubble_layout.addWidget(you)
        bubble_layout.addWidget(prompt)
        return bubble

    def set_live(self, live: bool) -> None:
        self.timeline_card.set_live(live)

    def set_reply(self, text: str, is_placeholder: bool = False) -> None:
        color = self._palette.text_secondary if is_placeholder else self._palette.text_primary
        self._dots.set_active(is_placeholder)
        if self.reply_label.text() != text:
            self.reply_label.setText(text)
        style = f"QLabel {{ color: {color}; font-size: {px(15)}px; background: transparent; border: none; }}"
        if self.reply_label.styleSheet() != style:
            self.reply_label.setStyleSheet(style)
