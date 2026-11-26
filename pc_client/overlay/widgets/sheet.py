"""Main Jarvis popup sheet window (appears under the mouse cursor on the PC)."""

from __future__ import annotations

import base64
from pathlib import Path
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QCursor, QDragEnterEvent, QDropEvent, QImage, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from overlay.api import AgentStreamProgress, AgentTurnResult, OverlayServerApi, PendingConfirmationInfo
from overlay.mic import MAX_RECORDING_S, PcMicRecorder
from overlay.placement import WorkArea, compute_popup_position
from overlay.session_store import AgentChatTurn, PcSessionStore, SavedAgentSession
from overlay.settings import OverlaySettings
from overlay.step_aside import (
    StepAsideCoordinator,
    current_foreground_hwnd,
    exclude_hwnd_from_capture,
    restore_foreground_hwnd,
)
from overlay.theme import OverlayPalette
from overlay.timeline import TimelineEntry
from overlay.widgets.chat import TurnWidget
from overlay.widgets.confirm_card import ConfirmCardWidget
from overlay.widgets.waveform import WaveformWidget

SHEET_WIDTH = 436
MAX_CHAT_SCROLL_HEIGHT = 350
MAX_ATTACHMENTS = 2
MAX_ATTACHMENT_WIDTH = 1280
AHK_QUICK_TOKENS = ("{Enter}", "{Tab}", "{Backspace}", "{Esc}", "{^a}", "{^c}", "{^v}")


class _UiBridge(QObject):
    amplitude = Signal(float)
    auto_stop = Signal()
    transcribed = Signal(str)
    transcribe_failed = Signal(str)
    send_done = Signal(str)
    send_failed = Signal(str)
    clients_loaded = Signal(list)
    agent_progress = Signal(object)
    agent_done = Signal(object, bool)
    agent_failed = Signal(str, bool)


class _PromptTextEdit(QTextEdit):
    """Multi-line input where Enter submits, Shift+Enter inserts a newline, and Ctrl+V can paste images."""

    def __init__(
        self,
        on_submit: Callable[[], None],
        on_paste_image: Callable[[QImage], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_submit = on_submit
        self._on_paste_image = on_paste_image

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self._on_submit()
            return
        if event.matches(Qt.KeySequence.StandardKey.Paste):
            clip = QApplication.clipboard()
            img = clip.image()
            if not img.isNull() and self._on_paste_image(img):
                event.accept()
                return
        super().keyPressEvent(event)


class JarvisSheetWindow(QWidget):
    """Frameless rounded popup matching Android AssistantActivity."""

    def __init__(
        self,
        palette: OverlayPalette,
        settings: OverlaySettings,
        session_store: PcSessionStore,
        api: OverlayServerApi,
        step_aside: StepAsideCoordinator,
        work_area_provider: Callable[[int, int], WorkArea],
        local_ahk_sender: Callable[[str], None],
        own_client_info_getter: Callable[[], tuple[str, str]],
        excluded_hwnds_getter: Callable[[], set[int]],
        on_runtime_phase_changed: Callable[[str, str, bool], None],
    ) -> None:
        super().__init__(None)
        self._palette = palette
        self._settings = settings
        self._session_store = session_store
        self._api = api
        self._step_aside = step_aside
        self._work_area_provider = work_area_provider
        self._local_ahk_sender = local_ahk_sender
        self._own_client_info_getter = own_client_info_getter
        self._excluded_hwnds_getter = excluded_hwnds_getter
        self._on_runtime_phase_changed = on_runtime_phase_changed

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setFixedWidth(SHEET_WIDTH)

        self._bridge = _UiBridge(self)
        self._connect_signals()

        self._ui_state = "review"
        self._runtime_phase = "idle"
        self._step_summary = ""
        self._is_keyboard_mode = settings.start_in_keyboard_mode
        self._is_agent_mode = settings.start_in_agent_mode
        self._session_id: str | None = session_store.resolve_initial_session_id(
            settings.agent_resume_timeout_s
        )
        self._connected_clients: list[dict[str, str]] = []
        self._selected_client_id: str = settings.selected_target_id
        self._recorder: PcMicRecorder | None = None
        self._record_start_at = 0.0
        self._pending_confirmation: PendingConfirmationInfo | None = None
        self._live_turn_widget: TurnWidget | None = None
        self._live_user_prompt = ""
        self._attachments: list[dict[str, Any]] = []
        self._stream_cancel_flag = threading.Event()
        self._paused = False
        self._pause_event = threading.Event()
        self._pause_event.set()

        self._record_timer = QTimer(self)
        self._record_timer.setInterval(250)
        self._record_timer.timeout.connect(self._update_recording_clock)

        self._build_ui()

    def hwnd(self) -> int:
        return int(self.winId())

    # ------------------------------------------------------------ UI construction

    def _build_ui(self) -> None:
        p = self._palette
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        self._card = QFrame(self)
        self._card.setObjectName("jarvisCard")
        self._card.setStyleSheet(
            f"QFrame#jarvisCard {{ background-color: {p.card_bg}; "
            f"border: 1px solid {p.divider}; border-radius: 26px; }}"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(20, 14, 20, 12)
        card_layout.setSpacing(8)
        outer.addWidget(self._card)

        # Header row
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self._state_label = QLabel("Listening", self._card)
        self._state_label.setStyleSheet(
            f"QLabel {{ color: {p.text_primary}; font-size: 19px; font-weight: bold; border: none; }}"
        )
        self._target_btn = QPushButton("Agent → This PC", self._card)
        self._target_btn.setFlat(True)
        self._target_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._target_btn.setStyleSheet(
            f"QPushButton {{ color: {p.text_secondary}; font-size: 13px; text-align: left; border: none; padding: 0; }}"
        )
        self._target_btn.clicked.connect(self._show_target_picker)
        titles.addWidget(self._state_label)
        titles.addWidget(self._target_btn)
        header.addLayout(titles, 1)

        self._agent_btn = self._make_icon_btn("✦", "Toggle thinking Agent mode", self._toggle_agent_mode)
        self._mode_btn = self._make_icon_btn("⌨", "Switch between voice and keyboard", self._toggle_input_mode)
        self._close_btn = self._make_icon_btn("⌄", "Minimize or close", self._on_close_button)
        header.addWidget(self._agent_btn)
        header.addWidget(self._mode_btn)
        header.addWidget(self._close_btn)
        card_layout.addLayout(header)

        # Session bar
        self._session_bar = QWidget(self._card)
        sb_layout = QHBoxLayout(self._session_bar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(6)
        self._session_btn = QPushButton("New session (click to switch)", self._session_bar)
        self._session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._session_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p.pill_bg}; color: {p.text_primary}; "
            "border: none; border-radius: 13px; padding: 5px 12px; font-size: 12px; text-align: left; }}"
        )
        self._session_btn.clicked.connect(self._show_session_picker)
        self._new_session_btn = QPushButton("+ New", self._session_bar)
        self._new_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_session_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p.pill_bg}; color: {p.accent}; "
            "border: none; border-radius: 13px; padding: 5px 11px; font-size: 12px; font-weight: bold; }}"
        )
        self._new_session_btn.clicked.connect(self._start_fresh_session)
        self._attach_btn = QPushButton("+ Image", self._session_bar)
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setStyleSheet(
            f"QPushButton {{ background-color: {p.pill_bg}; color: {p.accent}; "
            "border: none; border-radius: 13px; padding: 5px 11px; font-size: 12px; font-weight: bold; }}"
        )
        self._attach_btn.clicked.connect(self._pick_image_attachment)
        sb_layout.addWidget(self._session_btn, 1)
        sb_layout.addWidget(self._attach_btn)
        sb_layout.addWidget(self._new_session_btn)
        card_layout.addWidget(self._session_bar)

        # Attachment chips row
        self._attachments_bar = QWidget(self._card)
        self._attachments_layout = QHBoxLayout(self._attachments_bar)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_layout.setSpacing(6)
        self._attachments_bar.setVisible(False)
        card_layout.addWidget(self._attachments_bar)

        # Stage widgets
        self._waveform = WaveformWidget(p, self._card)
        card_layout.addWidget(self._waveform)

        self._spinner = QProgressBar(self._card)
        self._spinner.setRange(0, 0)
        self._spinner.setFixedHeight(8)
        self._spinner.setTextVisible(False)
        self._spinner.setStyleSheet(
            f"QProgressBar {{ background-color: {p.card_elevated}; border-radius: 4px; border: none; }} "
            f"QProgressBar::chunk {{ background-color: {p.accent}; border-radius: 4px; }}"
        )
        card_layout.addWidget(self._spinner)

        self._input = _PromptTextEdit(self._send_transcript, self._add_qimage_attachment, self._card)
        self._input.setFixedHeight(92)
        self._input.setStyleSheet(
            f"QTextEdit {{ background-color: {p.card_elevated}; color: {p.text_primary}; "
            "border: none; border-radius: 14px; padding: 10px 12px; font-size: 14px; }}"
        )
        card_layout.addWidget(self._input)

        # Chat history scroll area
        self._chat_scroll = QScrollArea(self._card)
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._chat_container = QWidget(self._chat_scroll)
        self._chat_container.setStyleSheet("QWidget { background: transparent; }")
        self._chat_layout = QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(0, 0, 4, 0)
        self._chat_layout.setSpacing(10)

        self._turns_holder = QWidget(self._chat_container)
        self._turns_layout = QVBoxLayout(self._turns_holder)
        self._turns_layout.setContentsMargins(0, 0, 0, 0)
        self._turns_layout.setSpacing(10)
        self._chat_layout.addWidget(self._turns_holder)

        self._confirm_card = ConfirmCardWidget(p, self._submit_confirmation, self._chat_container)
        self._chat_layout.addWidget(self._confirm_card)
        self._chat_layout.addStretch(1)
        self._chat_scroll.setWidget(self._chat_container)
        self._chat_scroll.setMaximumHeight(MAX_CHAT_SCROLL_HEIGHT)
        card_layout.addWidget(self._chat_scroll)

        # Interject bar (during AGENT_THINKING)
        self._interject_bar = QWidget(self._card)
        ij_layout = QHBoxLayout(self._interject_bar)
        ij_layout.setContentsMargins(0, 0, 0, 0)
        ij_layout.setSpacing(6)
        self._interject_input = QLineEdit(self._interject_bar)
        self._interject_input.setPlaceholderText("Interject or correct the agent…")
        self._interject_input.setStyleSheet(
            f"QLineEdit {{ background-color: {p.card_elevated}; color: {p.text_primary}; "
            "border: none; border-radius: 12px; padding: 8px 12px; font-size: 13px; }}"
        )
        self._interject_input.textEdited.connect(self._on_interject_text_edited)
        self._interject_input.returnPressed.connect(self._submit_interjection)
        ij_send = QPushButton("➤", self._interject_bar)
        ij_send.setCursor(Qt.CursorShape.PointingHandCursor)
        ij_send.setFixedSize(36, 36)
        ij_send.setStyleSheet(
            f"QPushButton {{ background-color: {p.pill_bg}; color: {p.accent}; "
            "border: none; border-radius: 18px; font-size: 15px; font-weight: bold; }}"
        )
        ij_send.clicked.connect(self._submit_interjection)
        ij_layout.addWidget(self._interject_input, 1)
        ij_layout.addWidget(ij_send)
        card_layout.addWidget(self._interject_bar)

        # Error banner
        self._error_label = QLabel("", self._card)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(
            f"QLabel {{ color: {p.error}; font-size: 12px; border: none; }}"
        )
        self._error_label.setVisible(False)
        card_layout.addWidget(self._error_label)

        # AHK quick tokens bar (dictation review only)
        self._chips_bar = QWidget(self._card)
        chips_layout = QHBoxLayout(self._chips_bar)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)
        for token in AHK_QUICK_TOKENS:
            chip = QPushButton(token, self._chips_bar)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton {{ background-color: {p.pill_bg}; color: {p.text_primary}; "
                "border: none; border-radius: 11px; padding: 4px 8px; "
                "font-family: Consolas, monospace; font-size: 12px; }}"
            )
            chip.clicked.connect(lambda _checked=False, t=token: self._insert_token(t))
            chips_layout.addWidget(chip)
        chips_layout.addStretch(1)
        card_layout.addWidget(self._chips_bar)

        # Recording progress slider & timer
        self._progress_box = QWidget(self._card)
        pb_layout = QVBoxLayout(self._progress_box)
        pb_layout.setContentsMargins(0, 0, 0, 0)
        pb_layout.setSpacing(2)
        self._slider = QSlider(Qt.Orientation.Horizontal, self._progress_box)
        self._slider.setRange(0, int(MAX_RECORDING_S))
        self._slider.setEnabled(False)
        clock_row = QHBoxLayout()
        self._timer_label = QLabel("0:00", self._progress_box)
        self._timer_label.setStyleSheet(f"QLabel {{ color: {p.text_primary}; font-size: 12px; border: none; }}")
        max_label = QLabel("2:00", self._progress_box)
        max_label.setStyleSheet(f"QLabel {{ color: {p.text_primary}; font-size: 12px; border: none; }}")
        clock_row.addWidget(self._timer_label)
        clock_row.addStretch(1)
        clock_row.addWidget(max_label)
        pb_layout.addWidget(self._slider)
        pb_layout.addLayout(clock_row)
        card_layout.addWidget(self._progress_box)

        # Bottom transport controls (Left / Center / Right)
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 4, 0, 2)
        self._ctrl_left = self._make_transport_btn(42, 18)
        self._ctrl_center = self._make_transport_btn(56, 22, accent=True)
        self._ctrl_right = self._make_transport_btn(42, 18)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_left)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_center)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_right)
        ctrl_row.addStretch(1)
        card_layout.addLayout(ctrl_row)

    def _make_icon_btn(self, text: str, tooltip: str, callback: Callable[[], None]) -> QPushButton:
        btn = QPushButton(text, self._card)
        btn.setToolTip(tooltip)
        btn.setFixedSize(36, 36)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {self._palette.text_secondary}; "
            "border: none; border-radius: 18px; font-size: 18px; }}"
        )
        btn.clicked.connect(callback)
        return btn

    def _make_transport_btn(self, size: int, font_px: int, accent: bool = False) -> QPushButton:
        p = self._palette
        bg = p.accent if accent else p.pill_bg
        fg = p.accent_on if accent else p.text_primary
        btn = QPushButton("", self._card)
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; "
            f"border: none; border-radius: {size // 2}px; font-size: {font_px}px; font-weight: bold; }}"
        )
        return btn

    def _connect_signals(self) -> None:
        self._bridge.amplitude.connect(self._waveform.push_amplitude)
        self._bridge.auto_stop.connect(self._stop_and_transcribe)
        self._bridge.transcribed.connect(self._on_transcribed)
        self._bridge.transcribe_failed.connect(self._on_transcribe_failed)
        self._bridge.send_done.connect(self._on_send_done)
        self._bridge.send_failed.connect(self._on_send_failed)
        self._bridge.clients_loaded.connect(self._on_clients_loaded)
        self._bridge.agent_progress.connect(self._on_agent_progress)
        self._bridge.agent_done.connect(self._on_agent_done)
        self._bridge.agent_failed.connect(self._on_agent_failed)

    # ------------------------------------------------------------ Summon / Show / Hide

    def summon_at_cursor(self) -> None:
        """Summons the popup below the mouse cursor (or toggles stop/close if already open)."""
        if self.isVisible():
            if self._ui_state == "recording":
                self._stop_and_transcribe()
                return
            if self._ui_state in ("review", "agent_result"):
                self.dismiss_overlay()
                return

        prev_hwnd = current_foreground_hwnd(self._excluded_hwnds_getter())
        self._step_aside.state.on_overlay_shown(prev_hwnd)
        self._refresh_clients_async()

        if self._runtime_phase in ("thinking", "paused", "confirm"):
            self._position_near_cursor_and_show()
            self._notify_pill()
            return

        self._session_id = self._session_store.resolve_initial_session_id(
            self._settings.agent_resume_timeout_s
        )
        if self._is_keyboard_mode:
            self._enter_keyboard_mode(clear_text=True)
        else:
            self._start_recording()
        self._position_near_cursor_and_show()
        self._notify_pill()

    def reopen_from_pill(self, focus_interject: bool = False) -> None:
        prev_hwnd = current_foreground_hwnd(self._excluded_hwnds_getter())
        self._step_aside.state.on_overlay_shown(prev_hwnd)
        self._position_near_cursor_and_show()
        self._notify_pill()
        if focus_interject and self._interject_bar.isVisible():
            self._interject_input.setFocus()

    def hide_temporarily_for_pc_work(self) -> None:
        if self.isVisible():
            self.hide()

    def restore_after_pc_work(self) -> None:
        if not self.isVisible():
            self.show()
            self.raise_()
            exclude_hwnd_from_capture(self.hwnd())
            self._notify_pill()

    def _position_near_cursor_and_show(self) -> None:
        self.adjustSize()
        cursor = QCursor.pos()
        area = self._work_area_provider(cursor.x(), cursor.y())
        x, y = compute_popup_position(cursor.x(), cursor.y(), self.width(), self.height(), area)
        self.move(QPoint(x, y))
        self.show()
        self.raise_()
        self.activateWindow()
        exclude_hwnd_from_capture(self.hwnd())
        if self._ui_state == "review":
            self._input.setFocus()

    def _on_close_button(self) -> None:
        if self._runtime_phase in ("thinking", "paused", "confirm"):
            self.minimize_to_pill()
        else:
            self.dismiss_overlay()

    def minimize_to_pill(self) -> None:
        self._stop_recording_cleanup()
        self._step_aside.state.on_user_hid_overlay()
        self._session_store.record_overlay_closed()
        self.hide()
        self._notify_pill()

    def dismiss_overlay(self) -> None:
        if self._runtime_phase in ("thinking", "paused", "confirm"):
            self.minimize_to_pill()
            return
        self._stop_recording_cleanup()
        self._runtime_phase = "idle"
        self._step_aside.state.on_user_hid_overlay()
        self._session_store.record_overlay_closed()
        self.hide()
        self._notify_pill()

    def _notify_pill(self) -> None:
        self._on_runtime_phase_changed(self._runtime_phase, self._step_summary, self.isVisible())

    # ------------------------------------------------------------ Keyboard events (type-to-switch & Esc)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._on_close_button()
            return
        if self._ui_state == "recording":
            text = event.text()
            if text and text.isprintable():
                event.accept()
                self._enter_keyboard_mode(clear_text=True)
                self._input.insertPlainText(text)
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ Drag & drop image attachments

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._is_agent_mode and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if not self._is_agent_mode:
            return
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                img = QImage(local)
                if not img.isNull():
                    self._add_qimage_attachment(img, Path(local).name)

    # ------------------------------------------------------------ Modes & Attachments

    def _toggle_input_mode(self) -> None:
        if self._is_keyboard_mode:
            self._is_keyboard_mode = False
            self._start_recording()
        else:
            self._enter_keyboard_mode(clear_text=self._ui_state != "review")

    def _enter_keyboard_mode(self, clear_text: bool = False) -> None:
        self._is_keyboard_mode = True
        self._stop_recording_cleanup()
        self._clear_error()
        if clear_text:
            self._input.clear()
        self._set_ui_state("review")
        self._input.setFocus()

    def _toggle_agent_mode(self) -> None:
        self._is_agent_mode = not self._is_agent_mode
        self._settings.start_in_agent_mode = self._is_agent_mode
        self._settings.save()
        if self._ui_state == "agent_result" and not self._is_agent_mode:
            self._runtime_phase = "idle"
            self._set_ui_state("review")
        else:
            self._set_ui_state(self._ui_state)

    def _pick_image_attachment(self) -> None:
        if len(self._attachments) >= MAX_ATTACHMENTS:
            self._show_error(f"At most {MAX_ATTACHMENTS} images can be attached per prompt.")
            return
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            "Attach image to Agent prompt",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not file_path:
            return
        img = QImage(file_path)
        if img.isNull():
            self._show_error("Could not load the selected image.")
            return
        self._add_qimage_attachment(img, Path(file_path).name)

    def _add_qimage_attachment(self, img: QImage, label: str = "Pasted image") -> bool:
        if not self._is_agent_mode or len(self._attachments) >= MAX_ATTACHMENTS:
            return False
        scaled = (
            img.scaledToWidth(MAX_ATTACHMENT_WIDTH, Qt.TransformationMode.SmoothTransformation)
            if img.width() > MAX_ATTACHMENT_WIDTH
            else img
        )
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "JPEG", 82)
        buf.close()
        raw_bytes = bytes(ba)
        if not raw_bytes:
            return False
        self._attachments.append(
            {
                "mime_type": "image/jpeg",
                "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
                "label": label[:50],
                "width": scaled.width(),
                "height": scaled.height(),
            }
        )
        self._render_attachment_chips()
        return True

    def _render_attachment_chips(self) -> None:
        while self._attachments_layout.count() > 0:
            item = self._attachments_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for idx, att in enumerate(self._attachments):
            btn = QPushButton(f"🖼 {att['label']} ✕", self._attachments_bar)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {self._palette.pill_bg}; color: {self._palette.accent}; "
                "border: none; border-radius: 11px; padding: 3px 10px; font-size: 12px; }}"
            )
            btn.clicked.connect(lambda _c=False, i=idx: self._remove_attachment(i))
            self._attachments_layout.addWidget(btn)
        self._attachments_layout.addStretch(1)
        self._attachments_bar.setVisible(bool(self._attachments) and self._is_agent_mode)
        self.adjustSize()

    def _remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            self._attachments.pop(index)
            self._render_attachment_chips()

    def _insert_token(self, token: str) -> None:
        self._input.insertPlainText(token)
        self._input.setFocus()

    # ------------------------------------------------------------ Recording & Transcription

    def _start_recording(self) -> None:
        self._is_keyboard_mode = False
        self._clear_error()
        self._stop_recording_cleanup()
        self._waveform.reset()
        recorder = PcMicRecorder(
            on_amplitude=lambda lvl: self._bridge.amplitude.emit(lvl),
            on_auto_stop=lambda: self._bridge.auto_stop.emit(),
            auto_stop=self._settings.auto_stop_on_silence,
            silence_duration_s=self._settings.silence_duration_s,
        )
        try:
            recorder.start()
        except Exception as exc:
            self._show_error(str(exc))
            self._enter_keyboard_mode(clear_text=False)
            return
        self._recorder = recorder
        self._record_start_at = time.monotonic()
        self._update_recording_clock()
        self._waveform.set_active(True)
        self._record_timer.start()
        self._set_ui_state("recording")

    def _stop_recording_cleanup(self) -> None:
        self._record_timer.stop()
        self._waveform.set_active(False)
        if self._recorder is not None:
            self._recorder.cancel()
            self._recorder = None

    def _stop_and_transcribe(self) -> None:
        if self._ui_state != "recording" or self._recorder is None:
            return
        self._record_timer.stop()
        self._waveform.set_active(False)
        recorder = self._recorder
        self._recorder = None
        wav_bytes = recorder.stop()
        if len(wav_bytes) <= 44:
            self._show_error("No audio captured.")
            self._set_ui_state("review")
            return
        self._clear_error()
        self._set_ui_state("transcribing")
        lang = self._settings.language

        def _worker() -> None:
            try:
                text = self._api.transcribe_wav(wav_bytes, lang)
                self._bridge.transcribed.emit(text)
            except Exception as exc:
                self._bridge.transcribe_failed.emit(str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_transcribed(self, text: str) -> None:
        self._input.setPlainText(text)
        cursor = self._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._input.setTextCursor(cursor)
        self._set_ui_state("review")
        self._input.setFocus()

    def _on_transcribe_failed(self, err: str) -> None:
        self._show_error(err or "Transcription failed")
        self._set_ui_state("review")

    def _update_recording_clock(self) -> None:
        elapsed = max(0, int(time.monotonic() - self._record_start_at))
        self._timer_label.setText(f"{elapsed // 60}:{elapsed % 60:02d}")
        self._slider.setValue(min(elapsed, int(MAX_RECORDING_S)))

    # ------------------------------------------------------------ Send Dictation or Agent Prompt

    def _send_transcript(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            self._show_error("Enter or dictate some text before sending.")
            return
        if self._is_agent_mode:
            self._start_agent_turn(text, interjection=False)
            return
        self._send_dictation(text)

    def _send_dictation(self, text: str) -> None:
        self._clear_error()
        own_id, own_name = self._own_client_info_getter()
        target_id = self._effective_target_id()
        is_local_pc = not target_id or target_id == own_id
        self._set_ui_state("sending")

        if is_local_pc:
            prev_hwnd = self._step_aside.state.remembered_hwnd
            self._step_aside.state.on_user_hid_overlay()
            self.hide()

            def _local_worker() -> None:
                try:
                    restore_foreground_hwnd(prev_hwnd)
                    time.sleep(0.15)
                    self._local_ahk_sender(text)
                    self._bridge.send_done.emit(own_name or "This PC")
                except Exception as exc:
                    self._bridge.send_failed.emit(str(exc))

            threading.Thread(target=_local_worker, daemon=True).start()
            return

        def _remote_worker() -> None:
            try:
                cname = self._api.send_keystrokes(text, target_id)
                self._bridge.send_done.emit(cname)
            except Exception as exc:
                self._bridge.send_failed.emit(str(exc))

        threading.Thread(target=_remote_worker, daemon=True).start()

    def _on_send_done(self, _client_name: str) -> None:
        self._input.clear()
        self.dismiss_overlay()

    def _on_send_failed(self, err: str) -> None:
        if not self.isVisible():
            self.show()
        self._show_error(err or "Failed to send keystrokes")
        self._set_ui_state("review")

    # ------------------------------------------------------------ Agent streaming & Confirmation

    def _start_agent_turn(self, prompt: str, interjection: bool = False) -> None:
        self._clear_error()
        self._stream_cancel_flag.set()
        self._stream_cancel_flag = threading.Event()
        cancel_event = self._stream_cancel_flag
        self._paused = False
        self._pause_event.set()

        saved = self._session_store.get_session(self._session_id)
        history = saved.to_history_messages() if saved is not None else []
        prior_turns = list(saved.turns) if saved is not None else []
        attachments = list(self._attachments)
        self._attachments.clear()
        self._render_attachment_chips()

        self._live_user_prompt = prompt
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        self._prepare_live_turn(prior_turns, prompt)
        self._runtime_phase = "thinking"
        self._step_summary = "Redirecting…" if interjection else "Thinking…"
        self._set_ui_state("agent_thinking")
        self._notify_pill()

        target_id = self._effective_target_id()
        sid = self._session_id

        def _worker() -> None:
            try:
                result = self._api.run_agent_stream(
                    prompt=prompt,
                    client_id=target_id,
                    session_id=sid,
                    history=history,
                    interjection=interjection,
                    attachments=attachments,
                    on_progress=lambda prog: self._bridge.agent_progress.emit(prog),
                    is_cancelled=cancel_event.is_set,
                )
                if not cancel_event.is_set():
                    self._bridge.agent_done.emit(result, False)
            except InterruptedError:
                pass
            except Exception as exc:
                if not cancel_event.is_set():
                    self._bridge.agent_failed.emit(str(exc), False)

        threading.Thread(target=_worker, daemon=True).start()

    def _submit_confirmation(self, approved: bool, rejection_reason: str | None) -> None:
        pending = self._pending_confirmation
        sid = self._session_id
        if pending is None or not sid:
            return
        self._clear_error()
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        self._stream_cancel_flag.set()
        self._stream_cancel_flag = threading.Event()
        cancel_event = self._stream_cancel_flag
        self._paused = False
        self._pause_event.set()

        saved = self._session_store.get_session(sid)
        prior_turns = list(saved.turns[:-1]) if (saved and saved.turns) else []
        last_turn = saved.turns[-1] if (saved and saved.turns) else None
        initial_tl = list(last_turn.timeline) if last_turn is not None else []
        prompt = last_turn.user_prompt if last_turn is not None else self._live_user_prompt
        self._live_user_prompt = prompt
        self._prepare_live_turn(prior_turns, prompt, initial_tl)

        self._runtime_phase = "thinking"
        self._step_summary = "Thinking…"
        self._set_ui_state("agent_thinking")
        self._notify_pill()
        target_id = self._effective_target_id()

        def _worker() -> None:
            try:
                result = self._api.confirm_agent_stream(
                    session_id=sid,
                    confirmation_id=pending.confirmation_id,
                    approved=approved,
                    client_id=target_id,
                    initial_timeline=initial_tl,
                    rejection_reason=rejection_reason,
                    on_progress=lambda prog: self._bridge.agent_progress.emit(prog),
                    is_cancelled=cancel_event.is_set,
                )
                if not cancel_event.is_set():
                    self._bridge.agent_done.emit(result, True)
            except InterruptedError:
                pass
            except Exception as exc:
                if not cancel_event.is_set():
                    self._bridge.agent_failed.emit(str(exc), True)

        threading.Thread(target=_worker, daemon=True).start()

    def _prepare_live_turn(
        self,
        existing_turns: list[AgentChatTurn],
        user_prompt: str,
        initial_timeline: list[TimelineEntry] | None = None,
    ) -> None:
        self._clear_turns_layout()
        for turn in existing_turns:
            tw = TurnWidget(self._palette, turn.userPrompt if hasattr(turn, "userPrompt") else turn.user_prompt, turn.reply)
            tw.timeline_card.sync_entries(turn.timeline)
            self._turns_layout.addWidget(tw)
        live = TurnWidget(
            self._palette,
            user_prompt,
            "Thinking…",
            expand_timeline=True,
            is_placeholder_reply=True,
        )
        if initial_timeline:
            live.timeline_card.sync_entries(initial_timeline)
        self._turns_layout.addWidget(live)
        self._live_turn_widget = live
        self._scroll_chat_to_bottom()

    def _on_agent_progress(self, progress: AgentStreamProgress) -> None:
        if progress.session_id:
            self._session_id = progress.session_id
        if self._live_turn_widget is not None:
            self._live_turn_widget.timeline_card.sync_entries(progress.timeline)
            if progress.reply.strip():
                self._live_turn_widget.set_reply(progress.reply, is_placeholder=False)
        self._step_summary = self._derive_step_summary(progress.timeline, progress.reply)
        self._notify_pill()
        self._scroll_chat_to_bottom()

    def _on_agent_done(self, result: AgentTurnResult, is_resume: bool) -> None:
        resolved_sid = result.session_id or self._session_id or self._session_store.start_new_session()
        self._session_id = resolved_sid
        reply_text = result.reply.strip() or "Done."
        turn = AgentChatTurn(
            user_prompt=self._live_user_prompt,
            reply=reply_text,
            timeline=result.timeline,
        )
        if is_resume:
            saved = self._session_store.update_last_turn(resolved_sid, turn)
        else:
            saved = self._session_store.record_turn(resolved_sid, turn)

        self._live_turn_widget = None
        self._render_saved_session(saved, expand_last=True)
        if result.pending_confirmation is not None:
            self._pending_confirmation = result.pending_confirmation
            self._confirm_card.bind_confirmation(result.pending_confirmation)
            self._runtime_phase = "confirm"
            self._step_summary = "Confirm action"
        else:
            self._pending_confirmation = None
            self._confirm_card.bind_confirmation(None)
            self._runtime_phase = "completed"
            self._step_summary = "Done"
        self._set_ui_state("agent_result")
        self._notify_pill()
        self._scroll_chat_to_bottom()

    def _on_agent_failed(self, err: str, is_resume: bool) -> None:
        self._live_turn_widget = None
        self._runtime_phase = "error"
        self._step_summary = "Error"
        self._show_error(err or "Agent execution failed")
        saved = self._session_store.get_session(self._session_id)
        if is_resume and saved is not None:
            self._render_saved_session(saved, expand_last=True)
            self._set_ui_state("agent_result")
        else:
            if not self._input.toPlainText().strip() and self._live_user_prompt:
                self._input.setPlainText(self._live_user_prompt)
            self._set_ui_state("review")
        self._notify_pill()

    def toggle_pause_agent(self) -> None:
        if self._runtime_phase == "thinking":
            self._paused = True
            self._runtime_phase = "paused"
            self._step_summary = "Paused"
            self._set_ui_state("agent_thinking")
            self._notify_pill()
        elif self._runtime_phase == "paused":
            self._paused = False
            self._runtime_phase = "thinking"
            self._step_summary = "Thinking…"
            self._set_ui_state("agent_thinking")
            self._notify_pill()

    def stop_agent(self) -> None:
        self._stream_cancel_flag.set()
        self._paused = False
        self._runtime_phase = "idle"
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        saved = self._session_store.get_session(self._session_id)
        if saved is not None and saved.turns:
            self._render_saved_session(saved, expand_last=False)
            self._set_ui_state("agent_result")
        else:
            self._set_ui_state("review")
        self._notify_pill()

    def _on_interject_text_edited(self, text: str) -> None:
        if text.strip() and self._runtime_phase == "thinking":
            self.toggle_pause_agent()

    def _submit_interjection(self) -> None:
        text = self._interject_input.text().strip()
        if not text:
            return
        self._interject_input.clear()
        self._start_agent_turn(text, interjection=True)

    # ------------------------------------------------------------ Sessions & Targets

    def _render_saved_session(self, session: SavedAgentSession, expand_last: bool = False) -> None:
        self._clear_turns_layout()
        for idx, turn in enumerate(session.turns):
            expand = expand_last and (idx == len(session.turns) - 1)
            tw = TurnWidget(self._palette, turn.user_prompt, turn.reply, expand_timeline=expand)
            tw.timeline_card.sync_entries(turn.timeline)
            self._turns_layout.addWidget(tw)

    def _clear_turns_layout(self) -> None:
        while self._turns_layout.count() > 0:
            item = self._turns_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _show_session_picker(self) -> None:
        sessions = self._session_store.list_sessions()
        items = ["+ Start a new session"] + [
            f"{s.title} ({len(s.turns)} turns)" for s in sessions
        ]
        if sessions:
            items.append("🗑 Clear all session history")
        choice, ok = QInputDialog.getItem(
            self, "Agent chat session", "Select session:", items, 0, False
        )
        if not ok or not choice:
            return
        if choice == "+ Start a new session":
            self._start_fresh_session()
        elif choice.startswith("🗑"):
            self._session_store.clear_all()
            self._start_fresh_session()
        else:
            idx = items.index(choice) - 1
            if 0 <= idx < len(sessions):
                chosen = sessions[idx]
                self._session_id = chosen.session_id
                self._session_store.active_session_id = chosen.session_id
                self._render_saved_session(chosen, expand_last=False)
                self._set_ui_state("agent_result")

    def _start_fresh_session(self) -> None:
        self._runtime_phase = "idle"
        self._session_id = self._session_store.start_new_session()
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        self._clear_turns_layout()
        if self._ui_state == "agent_result":
            self._enter_keyboard_mode(clear_text=True)
        else:
            self._update_session_bar()
        self._notify_pill()

    def _refresh_clients_async(self) -> None:
        def _worker() -> None:
            try:
                clients = self._api.fetch_clients()
            except Exception:
                clients = []
            self._bridge.clients_loaded.emit(clients)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_clients_loaded(self, clients: list[dict[str, str]]) -> None:
        self._connected_clients = clients
        own_id, _own_name = self._own_client_info_getter()
        if not self._selected_client_id and own_id:
            self._selected_client_id = own_id
        elif self._selected_client_id and not any(c["id"] == self._selected_client_id for c in clients):
            self._selected_client_id = own_id or (clients[-1]["id"] if clients else "")
        self._update_subtitle()

    def _effective_target_id(self) -> str | None:
        own_id, _own_name = self._own_client_info_getter()
        chosen = self._selected_client_id or own_id
        return chosen or None

    def _show_target_picker(self) -> None:
        if not self._connected_clients:
            self._refresh_clients_async()
            return
        own_id, _own_name = self._own_client_info_getter()
        labels: list[str] = []
        ids: list[str] = []
        for c in self._connected_clients:
            suffix = " (This PC)" if c["id"] == own_id else ""
            labels.append(f"{c['name']}{suffix}")
            ids.append(c["id"])
        curr_idx = ids.index(self._selected_client_id) if self._selected_client_id in ids else 0
        choice, ok = QInputDialog.getItem(self, "Target PC", "Send to:", labels, curr_idx, False)
        if ok and choice in labels:
            self._selected_client_id = ids[labels.index(choice)]
            self._settings.selected_target_id = self._selected_client_id
            self._settings.save()
            self._update_subtitle()

    # ------------------------------------------------------------ State rendering

    def _set_ui_state(self, state: str) -> None:
        self._ui_state = state
        p = self._palette
        self._state_label.setText(self._state_title(state))
        self._update_subtitle()
        self._update_session_bar()
        self._render_attachment_chips()

        agent_color = p.accent if self._is_agent_mode else p.text_secondary
        self._agent_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {agent_color}; border: none; font-size: 18px; }}"
        )
        self._mode_btn.setText("🎤" if self._is_keyboard_mode else "⌨")
        self._mode_btn.setVisible(state in ("recording", "review"))
        self._agent_btn.setVisible(state in ("recording", "review", "agent_result"))

        self._waveform.setVisible(state == "recording")
        self._progress_box.setVisible(state == "recording")
        self._spinner.setVisible(state in ("transcribing", "sending"))
        self._input.setVisible(state in ("review", "sending"))
        self._input.setPlaceholderText(
            "Ask the Agent (PC audio, layout, apps, or phone controls)… (Enter to send, Shift+Enter newline)"
            if self._is_agent_mode
            else "Transcript (supports AHK keys like {Enter})…"
        )
        self._chips_bar.setVisible(state in ("review", "sending") and not self._is_agent_mode)
        self._chat_scroll.setVisible(state in ("agent_thinking", "agent_result"))
        self._interject_bar.setVisible(state == "agent_thinking")
        self._configure_transport_controls(state)
        self.adjustSize()

    def _state_title(self, state: str) -> str:
        if state == "recording":
            return "Listening"
        if state == "transcribing":
            return "Transcribing"
        if state == "review":
            return "Type a message" if self._is_keyboard_mode else "Review & edit"
        if state == "sending":
            return "Sending…"
        if state == "agent_thinking":
            return "Paused" if self._paused else "Thinking…"
        if state == "agent_result":
            return "Confirm action" if self._pending_confirmation else "Agent response"
        return "LocalWhisper"

    def _update_subtitle(self) -> None:
        own_id, own_name = self._own_client_info_getter()
        target_id = self._effective_target_id()
        matched = next((c["name"] for c in self._connected_clients if c["id"] == target_id), "")
        if not matched:
            matched = own_name or "This PC"
        elif target_id == own_id:
            matched = f"{matched} (This PC)"
        prefix = "Agent →" if self._is_agent_mode else "Target:"
        self._target_btn.setText(f"{prefix} {matched}")

    def _update_session_bar(self) -> None:
        show = self._is_agent_mode and self._ui_state not in ("sending",)
        self._session_bar.setVisible(show)
        if not show:
            return
        active = self._session_store.get_session(self._session_id)
        if active is not None and active.turns:
            self._session_btn.setText(f"Session: {active.title} ({len(active.turns)})")
            self._new_session_btn.setVisible(True)
        else:
            self._session_btn.setText("New session (click to switch)")
            self._new_session_btn.setVisible(False)

    def _configure_transport_controls(self, state: str) -> None:
        for btn in (self._ctrl_left, self._ctrl_center, self._ctrl_right):
            try:
                btn.clicked.disconnect()
            except Exception:
                pass
            btn.setVisible(True)

        if state == "recording":
            self._bind_ctrl(self._ctrl_left, "✕", "Cancel", self.dismiss_overlay)
            self._bind_ctrl(self._ctrl_center, "■", "Stop and transcribe", self._stop_and_transcribe)
            self._bind_ctrl(self._ctrl_right, "↻", "Restart recording", self._start_recording)
        elif state in ("review", "sending"):
            left_icon = "🎤" if self._is_keyboard_mode else "↻"
            self._bind_ctrl(self._ctrl_left, left_icon, "Voice dictate", self._start_recording)
            self._bind_ctrl(self._ctrl_center, "➤", "Send", self._send_transcript)
            self._bind_ctrl(self._ctrl_right, "✕", "Close", self.dismiss_overlay)
        elif state == "agent_result":
            self._bind_ctrl(
                self._ctrl_left, "⌨", "Type follow-up", lambda: self._enter_keyboard_mode(clear_text=True)
            )
            self._bind_ctrl(self._ctrl_center, "🎤", "Voice follow-up", self._start_recording)
            self._bind_ctrl(self._ctrl_right, "✕", "Close", self.dismiss_overlay)
        elif state == "agent_thinking":
            p_icon = "▶" if self._paused else "⏸"
            self._bind_ctrl(self._ctrl_left, p_icon, "Pause / Resume", self.toggle_pause_agent)
            self._bind_ctrl(self._ctrl_center, "■", "Stop agent", self.stop_agent)
            self._bind_ctrl(self._ctrl_right, "⌄", "Minimize to corner", self.minimize_to_pill)
        else:
            self._ctrl_left.setVisible(False)
            self._bind_ctrl(self._ctrl_center, "✕", "Cancel", self.dismiss_overlay)
            self._ctrl_right.setVisible(False)

    @staticmethod
    def _bind_ctrl(btn: QPushButton, text: str, tooltip: str, callback: Callable[[], None]) -> None:
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.clicked.connect(callback)

    def _scroll_chat_to_bottom(self) -> None:
        QTimer.singleShot(
            20,
            lambda: self._chat_scroll.verticalScrollBar().setValue(
                self._chat_scroll.verticalScrollBar().maximum()
            ),
        )

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))
        self.adjustSize()

    def _clear_error(self) -> None:
        self._error_label.clear()
        self._error_label.setVisible(False)

    @staticmethod
    def _derive_step_summary(timeline: list[TimelineEntry], reply: str) -> str:
        for entry in reversed(timeline):
            if getattr(entry, "status", "") == "running":
                summary = getattr(entry, "summary", "")
                clean = summary.removeprefix("Running: ").removeprefix("Locating: ").strip()
                return clean[:28] if clean else "Working…"
        return "Responding…" if reply.strip() else "Thinking…"
