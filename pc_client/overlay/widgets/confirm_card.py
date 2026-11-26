"""Confirmation gate card widget for PC and remote phone actions."""

from __future__ import annotations

import base64
from typing import Callable

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

from overlay.api import PendingConfirmationInfo
from overlay.theme import OverlayPalette

MAX_PREVIEW_B64_CHARS = 600_000


class ConfirmCardWidget(QFrame):
    """Displays an action awaiting user approval with zoomed target preview and reject reason."""

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

        self.setStyleSheet(
            f"QFrame {{ background-color: {palette.card_elevated}; "
            f"border: 1.5px solid {palette.error}; border-radius: 14px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._title = QLabel("Approval required for PC action", self)
        self._title.setStyleSheet(
            f"QLabel {{ color: {palette.error}; font-weight: bold; font-size: 13px; border: none; }}"
        )
        layout.addWidget(self._title)

        self._desc = QLabel("", self)
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet(
            f"QLabel {{ color: {palette.text_primary}; font-size: 13px; border: none; }}"
        )
        layout.addWidget(self._desc)

        self._preview_btn = QPushButton(self)
        self._preview_btn.setFlat(True)
        self._preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._preview_btn.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self._preview_btn.clicked.connect(self._show_full_preview)
        self._preview_lbl = QLabel(self)
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_lbl.setStyleSheet("QLabel { border: none; }")
        self._preview_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self._preview_lbl.mousePressEvent = lambda _ev: self._show_full_preview()
        layout.addWidget(self._preview_lbl)

        self._reason_input = QLineEdit(self)
        self._reason_input.setPlaceholderText("Optional reason for rejecting…")
        self._reason_input.setStyleSheet(
            f"QLineEdit {{ background-color: {palette.card_bg}; color: {palette.text_primary}; "
            f"border: 1px solid {palette.divider}; border-radius: 10px; padding: 7px 10px; font-size: 13px; }}"
        )
        layout.addWidget(self._reason_input)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self._reject_btn = QPushButton("Reject", self)
        self._reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {palette.error}; "
            f"border: 1.5px solid {palette.error}; border-radius: 16px; padding: 6px 18px; font-weight: bold; }}"
        )
        self._reject_btn.clicked.connect(self._on_reject)

        self._approve_btn = QPushButton("Approve", self)
        self._approve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._approve_btn.setStyleSheet(
            f"QPushButton {{ background-color: {palette.accent}; color: {palette.accent_on}; "
            "border: none; border-radius: 16px; padding: 6px 18px; font-weight: bold; }"
        )
        self._approve_btn.clicked.connect(self._on_approve)

        btn_row.addWidget(self._reject_btn)
        btn_row.addWidget(self._approve_btn)
        layout.addLayout(btn_row)
        self.setVisible(False)

    def bind_confirmation(self, pending: PendingConfirmationInfo | None) -> None:
        if pending is None:
            self._full_pixmap = None
            self._preview_lbl.clear()
            self._preview_lbl.setVisible(False)
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
            scaled = pixmap.scaled(
                380,
                210,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_lbl.setPixmap(scaled)
            self._preview_lbl.setVisible(True)
        else:
            self._preview_lbl.clear()
            self._preview_lbl.setVisible(False)
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
        dlg = QDialog(self)
        dlg.setWindowTitle("What will be clicked")
        v = QVBoxLayout(dlg)
        lbl = QLabel(dlg)
        lbl.setPixmap(
            self._full_pixmap.scaled(
                920,
                680,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        v.addWidget(lbl)
        dlg.exec()

    @staticmethod
    def _decode_pixmap(b64: str) -> QPixmap | None:
        if not b64 or len(b64) > MAX_PREVIEW_B64_CHARS:
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None
        pix = QPixmap()
        return pix if pix.loadFromData(raw) else None
