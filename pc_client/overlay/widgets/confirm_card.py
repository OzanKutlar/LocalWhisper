"""Approval card for PC and remote phone actions, with a click-to-enlarge target preview."""

from __future__ import annotations

import base64
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from overlay.animations import PulseDot
from overlay.api import PendingConfirmationInfo
from overlay.style import px
from overlay.theme import OverlayPalette

MAX_PREVIEW_B64_CHARS = 600_000
PREVIEW_W = 520
PREVIEW_H = 300
FULL_PREVIEW_W = 1100
FULL_PREVIEW_H = 760


class _ClickableLabel(QLabel):
    def __init__(self, on_click: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(event)


class ConfirmCardWidget(QFrame):
    """Shows an action awaiting approval, its target preview and an optional rejection reason."""

    def __init__(
        self,
        palette: OverlayPalette,
        on_decision: Callable[[bool, str | None], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._on_decision = on_decision
        self._full_pixmap: QPixmap | None = None
        p = palette

        self.setObjectName("confirmCard")
        self.setStyleSheet(
            f"QFrame#confirmCard {{ background-color: {p.card_elevated}; "
            f"border: 2px solid {p.record_coral}; border-radius: {px(16)}px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(16), px(14), px(16), px(14))
        layout.setSpacing(px(10))

        title_row = QHBoxLayout()
        title_row.setSpacing(px(8))
        self._dot = PulseDot(p.record_coral, px(4), self)
        self._title = QLabel("Approval required for PC action", self)
        self._title.setStyleSheet(
            f"QLabel {{ color: {p.record_coral}; font-weight: 700; font-size: {px(14.5)}px; background: transparent; }}"
        )
        title_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self._title, 1)
        layout.addLayout(title_row)

        self._desc = QLabel("", self)
        self._desc.setTextFormat(Qt.TextFormat.PlainText)
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet(
            f"QLabel {{ color: {p.text_primary}; font-size: {px(15)}px; background: transparent; }}"
        )
        layout.addWidget(self._desc)

        self._preview = _ClickableLabel(self._show_full_preview, self)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setToolTip("Click to enlarge")
        self._preview.setStyleSheet(
            f"QLabel {{ background-color: {p.card_bg}; border-radius: {px(12)}px; padding: {px(4)}px; }}"
        )
        self._preview.setVisible(False)
        layout.addWidget(self._preview)

        self._reason_input = QLineEdit(self)
        self._reason_input.setPlaceholderText("Optional reason for rejecting…")
        self._reason_input.setStyleSheet(
            f"QLineEdit {{ background-color: {p.card_bg}; color: {p.text_primary}; "
            f"border: 1px solid {p.divider}; border-radius: {px(12)}px; padding: {px(8)}px {px(12)}px; "
            f"font-size: {px(14)}px; }}"
            f"QLineEdit:focus {{ border: 1px solid {p.accent}; }}"
        )
        layout.addWidget(self._reason_input)

        buttons = QHBoxLayout()
        buttons.setSpacing(px(10))
        buttons.addStretch(1)
        self._reject_btn = QPushButton("Reject", self)
        self._reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {p.error}; border: 2px solid {p.error}; "
            f"border-radius: {px(18)}px; padding: {px(7)}px {px(22)}px; font-size: {px(14)}px; font-weight: 700; }}"
            f"QPushButton:hover {{ background-color: {p.error_soft}; }}"
        )
        self._reject_btn.clicked.connect(self._on_reject)
        self._approve_btn = QPushButton("Approve", self)
        self._approve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._approve_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p.accent}; color: {p.accent_on}; border: 2px solid {p.accent}; "
            f"border-radius: {px(18)}px; padding: {px(7)}px {px(24)}px; font-size: {px(14)}px; font-weight: 700; }}"
            f"QPushButton:hover {{ border: 2px solid {p.accent_container}; }}"
        )
        self._approve_btn.clicked.connect(self._on_approve)
        buttons.addWidget(self._reject_btn)
        buttons.addWidget(self._approve_btn)
        layout.addLayout(buttons)
        self.setVisible(False)

    def bind_confirmation(self, pending: PendingConfirmationInfo | None) -> None:
        if pending is None:
            self._full_pixmap = None
            self._preview.clear()
            self._preview.setVisible(False)
            self._dot.set_pulsing(False)
            self.setVisible(False)
            return
        is_phone = pending.phone_action is not None
        self._title.setText(
            "Approval required for phone action" if is_phone else "Approval required for PC action"
        )
        self._desc.setText(pending.description)
        self._reason_input.clear()
        pixmap = self._decode_pixmap(pending.preview_image_b64)
        self._full_pixmap = pixmap
        if pixmap is not None and not pixmap.isNull():
            self._preview.setPixmap(
                pixmap.scaled(
                    px(PREVIEW_W),
                    px(PREVIEW_H),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self._preview.setVisible(True)
        else:
            self._preview.clear()
            self._preview.setVisible(False)
        self._dot.set_pulsing(True)
        self.setVisible(True)

    def focus_rejection_reason(self) -> None:
        self._reason_input.setFocus()

    def _on_approve(self) -> None:
        self._reason_input.clear()
        self._on_decision(True, None)

    def _on_reject(self) -> None:
        reason = self._reason_input.text().strip() or None
        self._reason_input.clear()
        self._on_decision(False, reason)

    def _show_full_preview(self) -> None:
        if self._full_pixmap is None or self._full_pixmap.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("What will be clicked")
        layout = QVBoxLayout(dialog)
        label = QLabel(dialog)
        label.setPixmap(
            self._full_pixmap.scaled(
                px(FULL_PREVIEW_W),
                px(FULL_PREVIEW_H),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        layout.addWidget(label)
        dialog.exec()

    @staticmethod
    def _decode_pixmap(b64: str) -> QPixmap | None:
        if not b64 or len(b64) > MAX_PREVIEW_B64_CHARS:
            return None
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError):
            return None
        pixmap = QPixmap()
        return pixmap if pixmap.loadFromData(raw) else None
