"""Main Jarvis popup sheet window (appears under the mouse cursor on the PC)."""

from __future__ import annotations

import base64
from pathlib import Path
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
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

from overlay.animations import (
    PulseDot,
    fade_in_window,
    fade_out_window,
    is_window_animating,
    stop_window_animation,
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
from overlay.style import px
from overlay.theme import OverlayPalette
from overlay.timeline import TimelineEntry
from overlay.widgets.chat import TurnWidget
from overlay.widgets.confirm_card import ConfirmCardWidget
from overlay.widgets.waveform import WaveformWidget

SHEET_WIDTH = 600
SHADOW_MARGIN = 24
CARD_RADIUS = 26
MAX_CHAT_HEIGHT = 560
MIN_CHAT_HEIGHT = 96
MAX_SCREEN_FRACTION = 0.62
FOLLOW_THRESHOLD = 28
MAX_STEP_SUMMARY_CHARS = 64
MAX_ATTACHMENTS = 2
MAX_ATTACHMENT_WIDTH = 1280
MAX_SCENE_CHIPS = 6
AHK_QUICK_TOKENS = ("{Enter}", "{Tab}", "{Backspace}", "{Esc}", "{^a}", "{^c}", "{^v}")
ACTIVE_RUNTIME_PHASES = ("thinking", "paused", "confirm")


def _button_style(
    bg: str,
    fg: str,
    hover: str,
    radius: float,
    pad_v: float,
    pad_h: float,
    font_px: float,
    weight: int = 600,
) -> str:
    return (
        f"QPushButton {{ background-color: {bg}; color: {fg}; border: 1px solid transparent; "
        f"border-radius: {px(radius)}px; padding: {px(pad_v)}px {px(pad_h)}px; "
        f"font-size: {px(font_px)}px; font-weight: {weight}; }}"
        f"QPushButton:hover {{ border: 1px solid {hover}; }}"
    )


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
    scenes_loaded = Signal(list)
    scene_needs_approval = Signal(str, str)
    scene_done = Signal(str, bool, str)


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
        self.setAcceptRichText(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self._on_submit()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            img = QApplication.clipboard().image()
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
        self.setFixedWidth(px(SHEET_WIDTH) + 2 * px(SHADOW_MARGIN))

        self._bridge = _UiBridge(self)
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
        self._scenes: list[dict[str, Any]] = []
        self._stream_cancel_flag = threading.Event()
        self._paused = False
        self._closing = False
        self._fit_pending = False
        self._follow_chat = True

        self._record_timer = QTimer(self)
        self._record_timer.setInterval(250)
        self._record_timer.timeout.connect(self._update_recording_clock)

        self._build_ui()
        self._connect_signals()

    def hwnd(self) -> int:
        return int(self.winId())

    # ------------------------------------------------------------ UI construction

    def _build_ui(self) -> None:
        p = self._palette
        margin = px(SHADOW_MARGIN)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(margin, margin - px(8), margin, margin + px(8))

        self._card = QFrame(self)
        self._card.setObjectName("jarvisCard")
        self._card.setStyleSheet(
            f"QFrame#jarvisCard {{ background-color: {p.card_bg}; border: 1px solid {p.divider}; "
            f"border-radius: {px(CARD_RADIUS)}px; }}"
        )
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(px(40))
        shadow.setOffset(0, px(10))
        shadow.setColor(QColor(0, 0, 0, 160 if p.is_dark else 70))
        self._card.setGraphicsEffect(shadow)
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(px(22), px(16), px(22), px(14))
        card_layout.setSpacing(px(10))
        outer.addWidget(self._card)

        self._build_header(card_layout)
        self._build_bars(card_layout)
        self._build_stage(card_layout)
        self._build_footer(card_layout)

    def _build_header(self, card_layout: QVBoxLayout) -> None:
        p = self._palette
        header = QHBoxLayout()
        header.setSpacing(px(4))
        titles = QVBoxLayout()
        titles.setSpacing(px(2))
        title_row = QHBoxLayout()
        title_row.setSpacing(px(8))
        self._state_dot = PulseDot(p.accent, px(5), self._card)
        self._state_label = QLabel("Listening", self._card)
        self._state_label.setStyleSheet(
            f"QLabel {{ color: {p.text_primary}; font-size: {px(21)}px; font-weight: 700; background: transparent; }}"
        )
        title_row.addWidget(self._state_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self._state_label, 1)
        self._target_btn = QPushButton("Agent → This PC", self._card)
        self._target_btn.setFlat(True)
        self._target_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._target_btn.setStyleSheet(
            f"QPushButton {{ color: {p.text_secondary}; font-size: {px(13.5)}px; text-align: left; "
            f"border: none; padding: 0px; background: transparent; }}"
            f"QPushButton:hover {{ color: {p.accent}; }}"
        )
        self._target_btn.clicked.connect(self._show_target_picker)
        titles.addLayout(title_row)
        titles.addWidget(self._target_btn)
        header.addLayout(titles, 1)

        self._agent_btn = self._make_icon_btn("✦", "Toggle thinking Agent mode", self._toggle_agent_mode)
        self._mode_btn = self._make_icon_btn("⌨", "Switch between voice and keyboard", self._toggle_input_mode)
        self._close_btn = self._make_icon_btn("⌄", "Minimize or close", self._on_close_button)
        for btn in (self._agent_btn, self._mode_btn, self._close_btn):
            header.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(header)

        self._step_label = QLabel("", self._card)
        self._step_label.setTextFormat(Qt.TextFormat.PlainText)
        self._step_label.setStyleSheet(
            f"QLabel {{ color: {p.accent}; background-color: {p.accent_container}; border-radius: {px(11)}px; "
            f"padding: {px(5)}px {px(12)}px; font-size: {px(13)}px; font-weight: 600; }}"
        )
        self._step_label.setVisible(False)
        card_layout.addWidget(self._step_label, 0, Qt.AlignmentFlag.AlignLeft)

    def _build_bars(self, card_layout: QVBoxLayout) -> None:
        p = self._palette
        self._session_bar = QWidget(self._card)
        sb_layout = QHBoxLayout(self._session_bar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(px(6))
        self._session_btn = QPushButton("New session (click to switch)", self._session_bar)
        self._session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._session_btn.setStyleSheet(
            _button_style(p.pill_bg, p.text_primary, p.divider, 14, 6, 13, 13, 500) + "QPushButton { text-align: left; }"
        )
        self._session_btn.clicked.connect(self._show_session_picker)
        self._attach_btn = QPushButton("+ Image", self._session_bar)
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setStyleSheet(_button_style(p.pill_bg, p.accent, p.accent, 14, 6, 12, 13))
        self._attach_btn.clicked.connect(self._pick_image_attachment)
        self._new_session_btn = QPushButton("+ New", self._session_bar)
        self._new_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_session_btn.setStyleSheet(_button_style(p.pill_bg, p.accent, p.accent, 14, 6, 12, 13))
        self._new_session_btn.clicked.connect(self._start_fresh_session)
        sb_layout.addWidget(self._session_btn, 1)
        sb_layout.addWidget(self._attach_btn)
        sb_layout.addWidget(self._new_session_btn)
        card_layout.addWidget(self._session_bar)

        self._attachments_bar = QWidget(self._card)
        self._attachments_layout = QHBoxLayout(self._attachments_bar)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_layout.setSpacing(px(6))
        self._attachments_bar.setVisible(False)
        card_layout.addWidget(self._attachments_bar)

        self._scenes_bar = QWidget(self._card)
        self._scenes_layout = QHBoxLayout(self._scenes_bar)
        self._scenes_layout.setContentsMargins(0, 0, 0, 0)
        self._scenes_layout.setSpacing(px(6))
        self._scenes_bar.setVisible(False)
        card_layout.addWidget(self._scenes_bar)

    def _build_stage(self, card_layout: QVBoxLayout) -> None:
        p = self._palette
        self._waveform = WaveformWidget(p, self._card)
        card_layout.addWidget(self._waveform)

        self._spinner = QProgressBar(self._card)
        self._spinner.setRange(0, 0)
        self._spinner.setFixedHeight(px(6))
        self._spinner.setTextVisible(False)
        self._spinner.setStyleSheet(
            f"QProgressBar {{ background-color: {p.card_elevated}; border-radius: {px(3)}px; border: none; }}"
            f"QProgressBar::chunk {{ background-color: {p.accent}; border-radius: {px(3)}px; }}"
        )
        card_layout.addWidget(self._spinner)

        self._input = _PromptTextEdit(self._send_transcript, self._add_qimage_attachment, self._card)
        self._input.setFixedHeight(px(112))
        self._input.setStyleSheet(
            f"QTextEdit {{ background-color: {p.card_elevated}; color: {p.text_primary}; "
            f"border: 1px solid transparent; border-radius: {px(16)}px; padding: {px(10)}px {px(12)}px; "
            f"font-size: {px(15.5)}px; }}"
            f"QTextEdit:focus {{ border: 1px solid {p.accent}; }}"
        )
        card_layout.addWidget(self._input)

        self._chat_scroll = QScrollArea(self._card)
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chat_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: transparent; width: {px(8)}px; margin: 0px; }}"
            f"QScrollBar::handle:vertical {{ background: {p.divider}; border-radius: {px(4)}px; min-height: {px(30)}px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {p.text_secondary}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )
        self._chat_container = QWidget(self._chat_scroll)
        self._chat_container.setObjectName("chatContainer")
        self._chat_container.setStyleSheet("QWidget#chatContainer { background: transparent; }")
        chat_layout = QVBoxLayout(self._chat_container)
        chat_layout.setContentsMargins(0, 0, px(8), 0)
        chat_layout.setSpacing(px(12))
        self._turns_holder = QWidget(self._chat_container)
        self._turns_layout = QVBoxLayout(self._turns_holder)
        self._turns_layout.setContentsMargins(0, 0, 0, 0)
        self._turns_layout.setSpacing(px(14))
        chat_layout.addWidget(self._turns_holder)
        self._confirm_card = ConfirmCardWidget(p, self._submit_confirmation, self._chat_container)
        chat_layout.addWidget(self._confirm_card)
        self._chat_scroll.setWidget(self._chat_container)
        self._chat_scroll.setFixedHeight(px(MIN_CHAT_HEIGHT))
        scrollbar = self._chat_scroll.verticalScrollBar()
        scrollbar.rangeChanged.connect(self._on_chat_range_changed)
        scrollbar.valueChanged.connect(self._on_chat_scrolled)
        card_layout.addWidget(self._chat_scroll)

        self._interject_bar = QWidget(self._card)
        ij_layout = QHBoxLayout(self._interject_bar)
        ij_layout.setContentsMargins(0, 0, 0, 0)
        ij_layout.setSpacing(px(8))
        self._interject_input = QLineEdit(self._interject_bar)
        self._interject_input.setPlaceholderText("Interject or correct the agent…")
        self._interject_input.setStyleSheet(
            f"QLineEdit {{ background-color: {p.card_elevated}; color: {p.text_primary}; "
            f"border: 1px solid transparent; border-radius: {px(14)}px; padding: {px(9)}px {px(14)}px; "
            f"font-size: {px(14)}px; }}"
            f"QLineEdit:focus {{ border: 1px solid {p.accent}; }}"
        )
        self._interject_input.textEdited.connect(self._on_interject_text_edited)
        self._interject_input.returnPressed.connect(self._submit_interjection)
        ij_send = QPushButton("➤", self._interject_bar)
        ij_send.setCursor(Qt.CursorShape.PointingHandCursor)
        ij_send.setFixedSize(px(40), px(40))
        ij_send.setStyleSheet(_button_style(p.accent, p.accent_on, p.accent_container, 20, 0, 0, 16, 700))
        ij_send.clicked.connect(self._submit_interjection)
        ij_layout.addWidget(self._interject_input, 1)
        ij_layout.addWidget(ij_send)
        card_layout.addWidget(self._interject_bar)

    def _build_footer(self, card_layout: QVBoxLayout) -> None:
        p = self._palette
        self._error_label = QLabel("", self._card)
        self._error_label.setWordWrap(True)
        self._error_label.setTextFormat(Qt.TextFormat.PlainText)
        self._error_label.setStyleSheet(
            f"QLabel {{ color: {p.error}; background-color: {p.error_soft}; border-radius: {px(10)}px; "
            f"padding: {px(6)}px {px(10)}px; font-size: {px(13)}px; }}"
        )
        self._error_label.setVisible(False)
        card_layout.addWidget(self._error_label)

        self._info_label = QLabel("", self._card)
        self._info_label.setWordWrap(True)
        self._info_label.setTextFormat(Qt.TextFormat.PlainText)
        self._info_label.setStyleSheet(
            f"QLabel {{ color: {p.text_secondary}; font-size: {px(13)}px; background: transparent; }}"
        )
        self._info_label.setVisible(False)
        card_layout.addWidget(self._info_label)

        self._chips_bar = QWidget(self._card)
        chips_layout = QHBoxLayout(self._chips_bar)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(px(6))
        for token in AHK_QUICK_TOKENS:
            chip = QPushButton(token, self._chips_bar)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(
                _button_style(p.pill_bg, p.text_primary, p.accent, 12, 4, 9, 12.5, 500)
                + "QPushButton { font-family: Consolas; }"
            )
            chip.clicked.connect(lambda _checked=False, t=token: self._insert_token(t))
            chips_layout.addWidget(chip)
        chips_layout.addStretch(1)
        card_layout.addWidget(self._chips_bar)

        self._progress_box = QWidget(self._card)
        pb_layout = QVBoxLayout(self._progress_box)
        pb_layout.setContentsMargins(0, 0, 0, 0)
        pb_layout.setSpacing(px(2))
        self._slider = QSlider(Qt.Orientation.Horizontal, self._progress_box)
        self._slider.setRange(0, int(MAX_RECORDING_S))
        self._slider.setEnabled(False)
        self._slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: {px(4)}px; background: {p.divider}; border-radius: {px(2)}px; }}"
            f"QSlider::sub-page:horizontal {{ background: {p.record_coral}; border-radius: {px(2)}px; }}"
            f"QSlider::handle:horizontal {{ background: {p.text_primary}; width: {px(12)}px; height: {px(12)}px; "
            f"margin: -{px(4)}px 0px; border-radius: {px(6)}px; }}"
        )
        clock_row = QHBoxLayout()
        clock_style = f"QLabel {{ color: {p.text_secondary}; font-size: {px(13)}px; background: transparent; }}"
        self._timer_label = QLabel("0:00", self._progress_box)
        self._timer_label.setStyleSheet(clock_style)
        max_label = QLabel("2:00", self._progress_box)
        max_label.setStyleSheet(clock_style)
        clock_row.addWidget(self._timer_label)
        clock_row.addStretch(1)
        clock_row.addWidget(max_label)
        pb_layout.addWidget(self._slider)
        pb_layout.addLayout(clock_row)
        card_layout.addWidget(self._progress_box)

        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, px(4), 0, px(2))
        self._ctrl_left = self._make_transport_btn(46, 18)
        self._ctrl_center = self._make_transport_btn(60, 23, accent=True)
        self._ctrl_right = self._make_transport_btn(46, 18)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_left)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_center)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._ctrl_right)
        ctrl_row.addStretch(1)
        card_layout.addLayout(ctrl_row)

    def _icon_btn_style(self, color: str) -> str:
        p = self._palette
        return (
            f"QPushButton {{ background: transparent; color: {color}; border: none; "
            f"border-radius: {px(19)}px; font-size: {px(18)}px; }}"
            f"QPushButton:hover {{ background-color: {p.pill_bg}; }}"
        )

    def _make_icon_btn(self, text: str, tooltip: str, callback: Callable[[], None]) -> QPushButton:
        btn = QPushButton(text, self._card)
        btn.setToolTip(tooltip)
        btn.setFixedSize(px(38), px(38))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(self._icon_btn_style(self._palette.text_secondary))
        btn.clicked.connect(callback)
        return btn

    def _make_transport_btn(self, size: int, font_px: int, accent: bool = False) -> QPushButton:
        p = self._palette
        bg = p.accent if accent else p.pill_bg
        fg = p.accent_on if accent else p.text_primary
        hover = p.accent_container if accent else p.accent
        btn = QPushButton("", self._card)
        btn.setFixedSize(px(size), px(size))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; border: 2px solid transparent; "
            f"border-radius: {px(size) // 2}px; font-size: {px(font_px)}px; font-weight: 700; }}"
            f"QPushButton:hover {{ border: 2px solid {hover}; }}"
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
        self._bridge.scenes_loaded.connect(self._on_scenes_loaded)
        self._bridge.scene_needs_approval.connect(self._on_scene_needs_approval)
        self._bridge.scene_done.connect(self._on_scene_done)

    # ------------------------------------------------------------ Summon / Show / Hide

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule_fit()

    def _is_shown(self) -> bool:
        return self.isVisible() and not self._closing

    def summon_at_cursor(self) -> None:
        """Summons the popup below the mouse cursor (or stops recording / closes if already open)."""
        if self._is_shown():
            if self._ui_state == "recording":
                self._stop_and_transcribe()
                return
            if self._ui_state in ("review", "agent_result"):
                self.dismiss_overlay()
                return

        prev_hwnd = current_foreground_hwnd(self._excluded_hwnds_getter())
        self._step_aside.state.on_overlay_shown(prev_hwnd)
        self._refresh_clients_async()
        self._refresh_scenes_async()

        if self._runtime_phase in ACTIVE_RUNTIME_PHASES:
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
        """Instant hide so the agent can screenshot or type into the window underneath."""
        stop_window_animation(self)
        self._closing = False
        if self.isVisible():
            self.hide()
        self.setWindowOpacity(1.0)

    def restore_after_pc_work(self) -> None:
        if self.isVisible():
            return
        fade_in_window(self, self.pos(), duration_ms=180, slide_px=px(8), on_done=self._schedule_fit)
        self.raise_()
        exclude_hwnd_from_capture(self.hwnd())
        self._notify_pill()

    def _position_near_cursor_and_show(self) -> None:
        stop_window_animation(self)
        self._closing = False
        self.setWindowOpacity(1.0)
        self.adjustSize()
        cursor = QCursor.pos()
        area = self._work_area_provider(cursor.x(), cursor.y())
        x, y = compute_popup_position(
            cursor.x(),
            cursor.y(),
            self.width(),
            self.height(),
            area,
            cursor_offset_y=px(6),
            edge_margin=0,
        )
        target = QPoint(x, y)
        if self.isVisible():
            self.move(target)
        else:
            fade_in_window(self, target, duration_ms=220, slide_px=px(16), on_done=self._schedule_fit)
        self.raise_()
        self.activateWindow()
        exclude_hwnd_from_capture(self.hwnd())
        self._schedule_fit()
        if self._ui_state == "review":
            self._input.setFocus()

    def _hide_animated(self) -> None:
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        fade_out_window(self, duration_ms=150, slide_px=px(10), on_done=self._on_hidden)

    def _on_hidden(self) -> None:
        self._closing = False
        self._notify_pill()

    def _on_close_button(self) -> None:
        if self._runtime_phase in ACTIVE_RUNTIME_PHASES:
            self.minimize_to_pill()
        else:
            self.dismiss_overlay()

    def minimize_to_pill(self) -> None:
        self._stop_recording_cleanup()
        self._step_aside.state.on_user_hid_overlay()
        self._session_store.record_overlay_closed()
        self._hide_animated()
        self._notify_pill()

    def dismiss_overlay(self) -> None:
        if self._runtime_phase in ACTIVE_RUNTIME_PHASES:
            self.minimize_to_pill()
            return
        self._stop_recording_cleanup()
        self._runtime_phase = "idle"
        self._step_aside.state.on_user_hid_overlay()
        self._session_store.record_overlay_closed()
        self._hide_animated()
        self._notify_pill()

    def _notify_pill(self) -> None:
        self._on_runtime_phase_changed(self._runtime_phase, self._step_summary, self._is_shown())

    # ------------------------------------------------------------ Sizing & scrolling

    def _schedule_fit(self) -> None:
        if self._fit_pending:
            return
        self._fit_pending = True
        QTimer.singleShot(0, self._fit_to_content)

    def _fit_to_content(self) -> None:
        self._fit_pending = False
        if self._chat_scroll.isVisibleTo(self):
            self._chat_scroll.setFixedHeight(self._desired_chat_height())
        self.adjustSize()
        self._keep_on_screen()

    def _desired_chat_height(self) -> int:
        viewport_w = self._chat_scroll.viewport().width()
        if viewport_w < px(200):
            viewport_w = px(SHEET_WIDTH) - px(60)
        layout = self._chat_container.layout()
        if layout.hasHeightForWidth():
            content_h = layout.heightForWidth(viewport_w)
        else:
            content_h = layout.sizeHint().height()
        center = self.geometry().center()
        area = self._work_area_provider(center.x(), center.y())
        cap = max(px(MIN_CHAT_HEIGHT), min(px(MAX_CHAT_HEIGHT), int(area.height * MAX_SCREEN_FRACTION)))
        return max(px(MIN_CHAT_HEIGHT), min(content_h + px(6), cap))

    def _keep_on_screen(self) -> None:
        if not self.isVisible() or is_window_animating(self):
            return
        margin = px(SHADOW_MARGIN)
        center = self.geometry().center()
        area = self._work_area_provider(center.x(), self.y() + margin)
        max_x = area.right - self.width() + margin
        max_y = area.bottom - self.height() + margin
        x = max(area.x - margin, min(self.x(), max_x))
        y = max(area.y - margin, min(self.y(), max_y))
        if x != self.x() or y != self.y():
            self.move(x, y)

    def _on_chat_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow_chat:
            self._chat_scroll.verticalScrollBar().setValue(maximum)

    def _on_chat_scrolled(self, value: int) -> None:
        scrollbar = self._chat_scroll.verticalScrollBar()
        self._follow_chat = value >= scrollbar.maximum() - px(FOLLOW_THRESHOLD)

    def _scroll_chat_to_bottom(self) -> None:
        self._follow_chat = True
        scrollbar = self._chat_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))

    # ------------------------------------------------------------ Keyboard, drag & drop

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

    # ------------------------------------------------------------ Modes & attachments

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
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "JPEG", 82)
        buf.close()
        raw_bytes = bytes(data)
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
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        p = self._palette
        for index, att in enumerate(self._attachments):
            btn = QPushButton(f"🖼 {att['label']}  ✕", self._attachments_bar)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_button_style(p.pill_bg, p.accent, p.error, 12, 4, 11, 12.5))
            btn.clicked.connect(lambda _c=False, i=index: self._remove_attachment(i))
            self._attachments_layout.addWidget(btn)
        self._attachments_layout.addStretch(1)
        self._attachments_bar.setVisible(bool(self._attachments) and self._is_agent_mode)
        self._schedule_fit()

    def _remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            self._attachments.pop(index)
            self._render_attachment_chips()

    def _insert_token(self, token: str) -> None:
        self._input.insertPlainText(token)
        self._input.setFocus()

    # ------------------------------------------------------------ Recording & transcription

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
        language = self._settings.language

        def _worker() -> None:
            try:
                text = self._api.transcribe_wav(wav_bytes, language)
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

    # ------------------------------------------------------------ Dictation

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
            self.hide_temporarily_for_pc_work()

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
                client_name = self._api.send_keystrokes(text, target_id)
                self._bridge.send_done.emit(client_name)
            except Exception as exc:
                self._bridge.send_failed.emit(str(exc))

        threading.Thread(target=_remote_worker, daemon=True).start()

    def _on_send_done(self, _client_name: str) -> None:
        self._input.clear()
        self.dismiss_overlay()

    def _on_send_failed(self, err: str) -> None:
        if not self.isVisible():
            fade_in_window(self, self.pos(), duration_ms=180, slide_px=px(8), on_done=self._schedule_fit)
        self._show_error(err or "Failed to send keystrokes")
        self._set_ui_state("review")

    # ------------------------------------------------------------ Agent streaming & confirmation

    def _start_agent_turn(self, prompt: str, interjection: bool = False) -> None:
        self._clear_error()
        self._show_info("")
        self._stream_cancel_flag.set()
        self._stream_cancel_flag = threading.Event()
        cancel_event = self._stream_cancel_flag
        self._paused = False

        saved = self._session_store.get_session(self._session_id)
        history = saved.to_history_messages() if saved is not None else []
        prior_turns = list(saved.turns) if saved is not None else []
        attachments = list(self._attachments)
        self._attachments.clear()
        self._render_attachment_chips()
        self._input.clear()

        self._live_user_prompt = prompt
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        self._prepare_live_turn(prior_turns, prompt)
        self._runtime_phase = "thinking"
        self._step_summary = "Redirecting…" if interjection else "Thinking…"
        self._set_ui_state("agent_thinking")
        self._notify_pill()

        target_id = self._effective_target_id()
        session_id = self._session_id

        def _worker() -> None:
            try:
                result = self._api.run_agent_stream(
                    prompt=prompt,
                    client_id=target_id,
                    session_id=session_id,
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
        session_id = self._session_id
        if pending is None or not session_id:
            return
        self._clear_error()
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        self._stream_cancel_flag.set()
        self._stream_cancel_flag = threading.Event()
        cancel_event = self._stream_cancel_flag
        self._paused = False

        saved = self._session_store.get_session(session_id)
        prior_turns = list(saved.turns[:-1]) if (saved and saved.turns) else []
        last_turn = saved.turns[-1] if (saved and saved.turns) else None
        initial_tl = list(last_turn.timeline) if last_turn is not None else []
        prompt = last_turn.user_prompt if last_turn is not None else self._live_user_prompt
        self._live_user_prompt = prompt
        self._prepare_live_turn(prior_turns, prompt, initial_tl)

        self._runtime_phase = "thinking"
        self._step_summary = "Running approved action…" if approved else "Thinking…"
        self._set_ui_state("agent_thinking")
        self._notify_pill()
        target_id = self._effective_target_id()

        def _worker() -> None:
            try:
                result = self._api.confirm_agent_stream(
                    session_id=session_id,
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
            widget = TurnWidget(self._palette, turn.user_prompt, turn.reply, parent=self._turns_holder)
            widget.timeline_card.sync_entries(turn.timeline)
            self._turns_layout.addWidget(widget)
        live = TurnWidget(
            self._palette,
            user_prompt,
            "Working on it…",
            expand_timeline=True,
            is_placeholder_reply=True,
            live=True,
            parent=self._turns_holder,
        )
        if initial_timeline:
            live.timeline_card.sync_entries(initial_timeline)
        self._turns_layout.addWidget(live)
        self._live_turn_widget = live
        self._scroll_chat_to_bottom()
        self._schedule_fit()

    def _on_agent_progress(self, progress: AgentStreamProgress) -> None:
        if progress.session_id:
            self._session_id = progress.session_id
        live = self._live_turn_widget
        if live is not None:
            live.timeline_card.sync_entries(progress.timeline)
            if progress.reply.strip():
                live.set_reply(progress.reply, is_placeholder=False)
        self._step_summary = self._derive_step_summary(progress.timeline, progress.reply)
        self._update_step_label()
        self._notify_pill()
        self._schedule_fit()

    def _on_agent_done(self, result: AgentTurnResult, is_resume: bool) -> None:
        resolved_id = result.session_id or self._session_id or self._session_store.start_new_session()
        self._session_id = resolved_id
        reply_text = result.reply.strip() or "Done."
        turn = AgentChatTurn(user_prompt=self._live_user_prompt, reply=reply_text, timeline=result.timeline)
        if is_resume:
            saved = self._session_store.update_last_turn(resolved_id, turn)
        else:
            saved = self._session_store.record_turn(resolved_id, turn)

        self._live_turn_widget = None
        self._render_saved_session(saved, expand_last=True)
        self._pending_confirmation = result.pending_confirmation
        self._confirm_card.bind_confirmation(result.pending_confirmation)
        if result.pending_confirmation is not None:
            self._runtime_phase = "confirm"
            self._step_summary = "Confirm action"
        else:
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
        elif self._runtime_phase == "paused":
            self._paused = False
            self._runtime_phase = "thinking"
            self._step_summary = "Thinking…"
        else:
            return
        self._set_ui_state("agent_thinking")
        self._notify_pill()

    def stop_agent(self) -> None:
        self._stream_cancel_flag.set()
        self._paused = False
        self._runtime_phase = "idle"
        self._live_turn_widget = None
        self._pending_confirmation = None
        self._confirm_card.bind_confirmation(None)
        saved = self._session_store.get_session(self._session_id)
        if saved is not None and saved.turns:
            self._render_saved_session(saved, expand_last=False)
            self._set_ui_state("agent_result")
        else:
            self._clear_turns_layout()
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

    # ------------------------------------------------------------ Sessions & targets

    def _render_saved_session(self, session: SavedAgentSession, expand_last: bool = False) -> None:
        self._clear_turns_layout()
        last_index = len(session.turns) - 1
        for index, turn in enumerate(session.turns):
            widget = TurnWidget(
                self._palette,
                turn.user_prompt,
                turn.reply,
                expand_timeline=expand_last and index == last_index,
                parent=self._turns_holder,
            )
            widget.timeline_card.sync_entries(turn.timeline)
            self._turns_layout.addWidget(widget)
        self._schedule_fit()

    def _clear_turns_layout(self) -> None:
        while self._turns_layout.count() > 0:
            item = self._turns_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_session_picker(self) -> None:
        sessions = self._session_store.list_sessions()
        items = ["+ Start a new session"] + [f"{s.title} ({len(s.turns)} turns)" for s in sessions]
        if sessions:
            items.append("🗑 Clear all session history")
        choice, ok = QInputDialog.getItem(self, "Agent chat session", "Select session:", items, 0, False)
        if not ok or not choice:
            return
        if choice == "+ Start a new session":
            self._start_fresh_session()
        elif choice.startswith("🗑"):
            self._session_store.clear_all()
            self._start_fresh_session()
        else:
            index = items.index(choice) - 1
            if 0 <= index < len(sessions):
                chosen = sessions[index]
                self._session_id = chosen.session_id
                self._session_store.active_session_id = chosen.session_id
                self._render_saved_session(chosen, expand_last=False)
                self._set_ui_state("agent_result")
                self._scroll_chat_to_bottom()

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
        for client in self._connected_clients:
            suffix = " (This PC)" if client["id"] == own_id else ""
            labels.append(f"{client['name']}{suffix}")
            ids.append(client["id"])
        current = ids.index(self._selected_client_id) if self._selected_client_id in ids else 0
        choice, ok = QInputDialog.getItem(self, "Target PC", "Send to:", labels, current, False)
        if ok and choice in labels:
            self._selected_client_id = ids[labels.index(choice)]
            self._settings.selected_target_id = self._selected_client_id
            self._settings.save()
            self._update_subtitle()

    # ------------------------------------------------------------ Scenes

    def _refresh_scenes_async(self) -> None:
        def _worker() -> None:
            try:
                scenes = self._api.fetch_scenes()
            except Exception:
                scenes = []
            self._bridge.scenes_loaded.emit(scenes)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scenes_loaded(self, scenes: list) -> None:
        self._scenes = [s for s in scenes if isinstance(s, dict) and s.get("name")]
        while self._scenes_layout.count() > 0:
            item = self._scenes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        p = self._palette
        for scene in self._scenes[:MAX_SCENE_CHIPS]:
            name = str(scene["name"])
            btn = QPushButton(f"▶ {name}", self._scenes_bar)
            btn.setToolTip(str(scene.get("description", "") or name))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_button_style(p.accent_container, p.accent, p.accent, 12, 4, 11, 12.5))
            btn.clicked.connect(lambda _checked=False, n=name: self._run_scene(n, approved=False))
            self._scenes_layout.addWidget(btn)
        self._scenes_layout.addStretch(1)
        self._update_scenes_bar_visibility()
        self._schedule_fit()

    def _update_scenes_bar_visibility(self) -> None:
        show = bool(self._scenes) and self._ui_state in ("recording", "review", "agent_result")
        self._scenes_bar.setVisible(show)

    def _run_scene(self, name: str, approved: bool) -> None:
        self._clear_error()
        self._show_info(f"Running scene '{name}'…")
        target_id = self._effective_target_id()

        def _worker() -> None:
            try:
                result = self._api.run_scene(name, approved=approved, client_id=target_id)
            except Exception as exc:
                self._bridge.scene_done.emit(name, False, str(exc))
                return
            if result.get("status") == "confirmation_required":
                description = str(result.get("description", "") or f"Run scene '{name}'?")
                self._bridge.scene_needs_approval.emit(name, description)
                return
            ok = bool(result.get("ok", False))
            message = f"Scene '{name}': {result.get('steps_ok', 0)}/{result.get('steps_run', 0)} steps OK"
            self._bridge.scene_done.emit(name, ok, message)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scene_needs_approval(self, name: str, description: str) -> None:
        self._show_info("")
        answer = QMessageBox.question(
            self,
            "Run scene?",
            description,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run_scene(name, approved=True)

    def _on_scene_done(self, _name: str, ok: bool, message: str) -> None:
        if ok:
            self._show_info(message)
            return
        self._show_info("")
        self._show_error(message or "Scene failed")

    # ------------------------------------------------------------ State rendering

    def _set_ui_state(self, state: str) -> None:
        self._ui_state = state
        p = self._palette
        self._state_label.setText(self._state_title(state))
        self._update_state_dot(state)
        self._update_subtitle()
        self._update_session_bar()
        self._render_attachment_chips()

        self._agent_btn.setStyleSheet(self._icon_btn_style(p.accent if self._is_agent_mode else p.text_secondary))
        self._mode_btn.setText("🎤" if self._is_keyboard_mode else "⌨")
        self._mode_btn.setVisible(state in ("recording", "review"))
        self._agent_btn.setVisible(state in ("recording", "review", "agent_result"))

        self._waveform.setVisible(state == "recording")
        self._progress_box.setVisible(state == "recording")
        self._spinner.setVisible(state in ("transcribing", "sending"))
        self._input.setVisible(state in ("review", "sending"))
        self._input.setPlaceholderText(
            "Ask the Agent anything… (Enter to send, Shift+Enter for a new line)"
            if self._is_agent_mode
            else "Transcript (supports AHK keys like {Enter})…"
        )
        self._chips_bar.setVisible(state in ("review", "sending") and not self._is_agent_mode)
        self._chat_scroll.setVisible(state in ("agent_thinking", "agent_result"))
        self._interject_bar.setVisible(state == "agent_thinking")
        self._update_step_label()
        self._update_scenes_bar_visibility()
        self._configure_transport_controls(state)
        self._schedule_fit()

    def _update_state_dot(self, state: str) -> None:
        p = self._palette
        if state == "recording":
            color, pulsing = p.record_coral, True
        elif state in ("transcribing", "sending"):
            color, pulsing = p.accent, True
        elif state == "agent_thinking":
            color, pulsing = (p.text_secondary, False) if self._paused else (p.accent, True)
        elif state == "agent_result":
            color, pulsing = (p.record_coral, True) if self._pending_confirmation else (p.success, False)
        else:
            color, pulsing = (p.accent if self._is_agent_mode else p.text_secondary), False
        self._state_dot.set_color(color)
        self._state_dot.set_pulsing(pulsing)

    def _update_step_label(self) -> None:
        show = self._ui_state == "agent_thinking" and bool(self._step_summary)
        if show:
            prefix = "⏸ " if self._paused else "⟳ "
            self._step_label.setText(prefix + self._step_summary)
        self._step_label.setVisible(show)

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
        show = self._is_agent_mode and self._ui_state != "sending"
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
            except (RuntimeError, TypeError):
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
            pause_icon = "▶" if self._paused else "⏸"
            self._bind_ctrl(self._ctrl_left, pause_icon, "Pause / Resume", self.toggle_pause_agent)
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

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))
        self._schedule_fit()

    def _clear_error(self) -> None:
        self._error_label.clear()
        self._error_label.setVisible(False)

    def _show_info(self, message: str) -> None:
        self._info_label.setText(message)
        self._info_label.setVisible(bool(message))
        self._schedule_fit()

    @staticmethod
    def _derive_step_summary(timeline: list[TimelineEntry], reply: str) -> str:
        for entry in reversed(timeline):
            if getattr(entry, "status", "") == "running":
                summary = str(getattr(entry, "summary", "") or "")
                clean = summary.removeprefix("Running: ").removeprefix("Locating: ").strip()
                return clean[:MAX_STEP_SUMMARY_CHARS] if clean else "Working…"
        return "Writing the reply…" if reply.strip() else "Thinking…"
