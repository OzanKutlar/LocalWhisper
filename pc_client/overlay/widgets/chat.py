"""Chat bubbles and collapsible thinking/tool timeline widget for the PC Jarvis popup."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from overlay.theme import OverlayPalette
from overlay.timeline import ThinkingEntry, TimelineEntry, ToolEntry, tool_count


def _tool_prefix(status: str) -> str:
    if status in ("ok", "queued_on_phone"):
        return "✓ "
    if status == "running":
        return "⟳ "
    if status == "pending_confirmation":
        return "⏳ "
    if status in ("rejected", "error"):
        return "✕ "
    return "• "


def _tool_color(status: str, palette: OverlayPalette) -> str:
    if status in ("ok", "queued_on_phone"):
        return palette.success
    if status in ("running", "pending_confirmation"):
        return palette.accent
    if status in ("rejected", "error"):
        return palette.error
    return palette.text_primary


class TimelineCardWidget(QFrame):
    """Collapsible card displaying interleaved <think> thoughts and tool status pills."""

    def __init__(self, palette: OverlayPalette, start_expanded: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._expanded = start_expanded
        self._entries: list[TimelineEntry] = []
        self.setStyleSheet(
            f"QFrame {{ background-color: {palette.card_elevated}; border-radius: 12px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        self._header = QPushButton(self)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFlat(True)
        self._header.setStyleSheet(
            f"QPushButton {{ color: {palette.accent}; font-weight: bold; font-size: 12px; text-align: left; border: none; }}"
        )
        self._header.clicked.connect(self._toggle)
        layout.addView = layout.addWidget
        layout.addWidget(self._header)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 0, 0)
        self._body_layout.setSpacing(4)
        self._body.setVisible(start_expanded)
        layout.addWidget(self._body)
        self.setVisible(False)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._update_header()

    def _update_header(self) -> None:
        count = tool_count(self._entries)
        action = "click to hide" if self._expanded else "click to view"
        self._header.setText(f"Thinking · {count} tool steps ({action})")

    def sync_entries(self, entries: list[TimelineEntry]) -> None:
        self._entries = list(entries)
        self.setVisible(bool(self._entries))
        self._update_header()
        if not self._expanded:
            return
        while self._body_layout.count() > 0:
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for entry in self._entries:
            if isinstance(entry, ThinkingEntry):
                lbl = QLabel(entry.text.strip(), self._body)
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                lbl.setStyleSheet(
                    f"QLabel {{ color: {self._palette.text_secondary}; font-family: Consolas, monospace; font-size: 12px; }}"
                )
                self._body_layout.addWidget(lbl)
            elif isinstance(entry, ToolEntry):
                color = _tool_color(entry.status, self._palette)
                pill = QLabel(_tool_prefix(entry.status) + entry.summary, self._body)
                pill.setWordWrap(True)
                pill.setStyleSheet(
                    f"QLabel {{ background-color: {self._palette.pill_bg}; color: {color}; "
                    "border-radius: 10px; padding: 4px 10px; font-size: 12px; }"
                )
                self._body_layout.addWidget(pill)


class TurnWidget(QWidget):
    """One user prompt + collapsible timeline + assistant reply box."""

    def __init__(
        self,
        palette: OverlayPalette,
        user_prompt: str,
        initial_reply: str,
        expand_timeline: bool = False,
        is_placeholder_reply: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        if user_prompt.strip():
            user_box = QFrame(self)
            user_box.setStyleSheet(
                f"QFrame {{ background-color: {palette.pill_bg}; border-radius: 14px; }}"
            )
            ub_layout = QVBoxLayout(user_box)
            ub_layout.setContentsMargins(14, 8, 14, 8)
            ub_layout.setSpacing(2)
            you_lbl = QLabel("You", user_box)
            you_lbl.setStyleSheet(f"QLabel {{ color: {palette.accent}; font-weight: bold; font-size: 11px; }}")
            prompt_lbl = QLabel(user_prompt, user_box)
            prompt_lbl.setWordWrap(True)
            prompt_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            prompt_lbl.setStyleSheet(f"QLabel {{ color: {palette.text_primary}; font-size: 14px; }}")
            ub_layout.addWidget(you_lbl)
            ub_layout.addWidget(prompt_lbl)
            layout.addWidget(user_box)

        self.timeline_card = TimelineCardWidget(palette, start_expanded=expand_timeline, parent=self)
        layout.addWidget(self.timeline_card)

        self.reply_label = QLabel(initial_reply, self)
        self.reply_label.setWordWrap(True)
        self.reply_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_reply(initial_reply, is_placeholder=is_placeholder_reply)
        layout.addWidget(self.reply_label)

    def set_reply(self, text: str, is_placeholder: bool = False) -> None:
        color = self._palette.text_secondary if is_placeholder else self._palette.text_primary
        self.reply_label.setText(text)
        self.reply_label.setStyleSheet(
            f"QLabel {{ background-color: {self._palette.card_elevated}; color: {color}; "
            "border-radius: 12px; padding: 11px 14px; font-size: 14px; }"
        )
