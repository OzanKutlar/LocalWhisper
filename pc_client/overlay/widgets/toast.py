"""Non-focus-stealing PC corner toast cards for mirrored Android notifications."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from overlay.placement import WorkArea
from overlay.step_aside import exclude_hwnd_from_capture
from overlay.theme import OverlayPalette

TOAST_WIDTH = 348
EDGE_MARGIN = 14
CARD_GAP = 10
AUTO_HIDE_MS = 14_000
MAX_VISIBLE_TOASTS = 4


class _ToastBridge(QObject):
    action_finished = Signal(str, bool, str)


class _SingleToastCard(QWidget):
    """One floating notification card with inline Reply and Dismiss on phone buttons."""

    def __init__(
        self,
        palette: OverlayPalette,
        notif_id: str,
        on_reply: Callable[[str, str], None],
        on_dismiss_phone: Callable[[str], None],
        on_close_local: Callable[[str], None],
    ) -> None:
        super().__init__(None)
        self._palette = palette
        self.notif_id = notif_id
        self._on_reply = on_reply
        self._on_dismiss_phone = on_dismiss_phone
        self._on_close_local = on_close_local

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedWidth(TOAST_WIDTH)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(lambda: self._on_close_local(self.notif_id))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self._frame = QFrame(self)
        self._frame.setObjectName("toastCard")
        self._frame.setStyleSheet(
            f"QFrame#toastCard {{ background-color: {palette.card_bg}; "
            f"border: 1px solid {palette.divider}; border-radius: 18px; }}"
        )
        outer.addWidget(self._frame)

        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(5)

        header = QHBoxLayout()
        self._app_label = QLabel("Phone", self._frame)
        self._app_label.setStyleSheet(
            f"QLabel {{ color: {palette.accent}; font-weight: bold; font-size: 11px; border: none; }}"
        )
        close_btn = QPushButton("✕", self._frame)
        close_btn.setFlat(True)
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ color: {palette.text_secondary}; border: none; font-size: 12px; }}"
        )
        close_btn.clicked.connect(lambda: self._on_close_local(self.notif_id))
        header.addWidget(self._app_label, 1)
        header.addWidget(close_btn)
        layout.addLayout(header)

        self._title_label = QLabel("", self._frame)
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(
            f"QLabel {{ color: {palette.text_primary}; font-weight: bold; font-size: 13px; border: none; }}"
        )
        layout.addWidget(self._title_label)

        self._body_label = QLabel("", self._frame)
        self._body_label.setWordWrap(True)
        self._body_label.setStyleSheet(
            f"QLabel {{ color: {palette.text_secondary}; font-size: 12px; border: none; }}"
        )
        layout.addWidget(self._body_label)

        self._reply_row = QWidget(self._frame)
        rr_layout = QHBoxLayout(self._reply_row)
        rr_layout.setContentsMargins(0, 4, 0, 0)
        rr_layout.setSpacing(6)
        self._reply_input = QLineEdit(self._reply_row)
        self._reply_input.setPlaceholderText("Reply on phone…")
        self._reply_input.setStyleSheet(
            f"QLineEdit {{ background-color: {palette.card_elevated}; color: {palette.text_primary}; "
            "border: none; border-radius: 10px; padding: 5px 9px; font-size: 12px; }"
        )
        self._reply_input.textEdited.connect(self._pause_auto_hide)
        self._reply_input.returnPressed.connect(self._send_reply)
        self._send_btn = QPushButton("➤", self._reply_row)
        self._send_btn.setFixedSize(28, 28)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setStyleSheet(
            f"QPushButton {{ background-color: {palette.accent}; color: {palette.accent_on}; "
            "border: none; border-radius: 14px; font-size: 12px; font-weight: bold; }"
        )
        self._send_btn.clicked.connect(self._send_reply)
        rr_layout.addWidget(self._reply_input, 1)
        rr_layout.addWidget(self._send_btn)
        layout.addWidget(self._reply_row)

        footer = QHBoxLayout()
        self._status_label = QLabel("", self._frame)
        self._status_label.setStyleSheet(
            f"QLabel {{ color: {palette.text_secondary}; font-size: 11px; border: none; }}"
        )
        dismiss_btn = QPushButton("Dismiss on phone", self._frame)
        dismiss_btn.setFlat(True)
        dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss_btn.setStyleSheet(
            f"QPushButton {{ color: {palette.text_secondary}; font-size: 11px; font-weight: bold; border: none; }}"
        )
        dismiss_btn.clicked.connect(lambda: self._on_dismiss_phone(self.notif_id))
        footer.addWidget(self._status_label, 1)
        footer.addWidget(dismiss_btn)
        layout.addLayout(footer)

    def hwnd(self) -> int:
        return int(self.winId())

    def bind_data(self, data: dict[str, Any]) -> None:
        app_name = str(data.get("app_name", "Phone") or "Phone")
        self._app_label.setText(f"📱 {app_name} · {self.notif_id}")
        self._title_label.setText(str(data.get("title", "") or "(no title)"))
        raw_msgs = data.get("messages")
        if isinstance(raw_msgs, list) and raw_msgs:
            body = "\n".join(str(m) for m in raw_msgs[-3:])
        else:
            body = str(data.get("text", "") or "")
        self._body_label.setText(body[:280])
        self._reply_row.setVisible(bool(data.get("can_reply", False)))
        self._status_label.clear()
        self.adjustSize()
        self._auto_timer.start(AUTO_HIDE_MS)

    def show_status(self, text: str) -> None:
        self._status_label.setText(text)

    def _pause_auto_hide(self, _text: str) -> None:
        self._auto_timer.stop()

    def _send_reply(self) -> None:
        text = self._reply_input.text().strip()
        if not text:
            return
        self._reply_input.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._status_label.setText("Sending…")
        self._on_reply(self.notif_id, text)


class PhoneNotificationToastManager:
    """Manages up to MAX_VISIBLE_TOASTS corner cards for mirrored phone notifications."""

    def __init__(
        self,
        palette: OverlayPalette,
        work_area_provider: Callable[[int, int], WorkArea],
        reply_fn: Callable[[str, str], None],
        dismiss_fn: Callable[[str], None],
        on_hwnds_changed: Callable[[], None],
    ) -> None:
        self._palette = palette
        self._work_area_provider = work_area_provider
        self._reply_fn = reply_fn
        self._dismiss_fn = dismiss_fn
        self._on_hwnds_changed = on_hwnds_changed
        self._cards: dict[str, _SingleToastCard] = {}
        self._bridge = _ToastBridge()
        self._bridge.action_finished.connect(self._on_action_finished)

    def active_hwnds(self) -> set[int]:
        return {card.hwnd() for card in self._cards.values()}

    def on_notification_event(self, event: str, data: dict[str, Any]) -> None:
        notif_id = str(data.get("id", "") or "").strip()
        if not notif_id:
            return
        if event == "notification_removed":
            self.close_card(notif_id)
            return
        if event != "notification_posted":
            return

        card = self._cards.get(notif_id)
        if card is None:
            while len(self._cards) >= MAX_VISIBLE_TOASTS:
                oldest_id = next(iter(self._cards.keys()))
                self.close_card(oldest_id)
            card = _SingleToastCard(
                self._palette,
                notif_id,
                on_reply=self._start_reply,
                on_dismiss_phone=self._start_dismiss,
                on_close_local=self.close_card,
            )
            self._cards[notif_id] = card

        card.bind_data(data)
        if not card.isVisible():
            card.show()
            exclude_hwnd_from_capture(card.hwnd())
            self._on_hwnds_changed()
        self._relayout()

    def close_card(self, notif_id: str) -> None:
        card = self._cards.pop(notif_id, None)
        if card is not None:
            card.hide()
            card.deleteLater()
            self._on_hwnds_changed()
            self._relayout()

    def close_all(self) -> None:
        for nid in list(self._cards.keys()):
            self.close_card(nid)

    def _relayout(self) -> None:
        area = self._work_area_provider(0, 0)
        cursor_y = area.bottom - EDGE_MARGIN
        for card in reversed(list(self._cards.values())):
            card.adjustSize()
            x = max(area.x + EDGE_MARGIN, area.right - card.width() - EDGE_MARGIN)
            y = max(area.y + EDGE_MARGIN, cursor_y - card.height())
            card.move(x, y)
            cursor_y = y - CARD_GAP

    def _start_reply(self, notif_id: str, text: str) -> None:
        def _worker() -> None:
            try:
                self._reply_fn(notif_id, text)
                self._bridge.action_finished.emit(notif_id, True, "Replied")
            except Exception as exc:
                self._bridge.action_finished.emit(notif_id, False, str(exc)[:60])

        threading.Thread(target=_worker, daemon=True).start()

    def _start_dismiss(self, notif_id: str) -> None:
        def _worker() -> None:
            try:
                self._dismiss_fn(notif_id)
                self._bridge.action_finished.emit(notif_id, True, "Dismissed")
            except Exception as exc:
                self._bridge.action_finished.emit(notif_id, False, str(exc)[:60])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_action_finished(self, notif_id: str, ok: bool, msg: str) -> None:
        if ok:
            self.close_card(notif_id)
        else:
            card = self._cards.get(notif_id)
            if card is not None:
                card.show_status(msg)
