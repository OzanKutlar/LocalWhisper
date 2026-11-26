"""Floating draggable corner pill for the PC Jarvis popup while the agent runs in the background."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from overlay.placement import WorkArea, compute_initial_pill_position, snap_pill_to_edge
from overlay.step_aside import exclude_hwnd_from_capture
from overlay.theme import OverlayPalette


class CornerPillWindow(QWidget):
    """Draggable always-on-top corner pill with quick actions (Pause/Resume, Interject, Open, Stop)."""

    def __init__(
        self,
        palette: OverlayPalette,
        work_area_provider: Callable[[int, int], WorkArea],
        on_toggle_pause: Callable[[], None],
        on_interject: Callable[[], None],
        on_open_sheet: Callable[[], None],
        on_stop: Callable[[], None],
        on_dismiss: Callable[[], None],
    ) -> None:
        super().__init__(None)
        self._palette = palette
        self._work_area_provider = work_area_provider
        self._on_toggle_pause = on_toggle_pause
        self._on_interject = on_interject
        self._on_open_sheet = on_open_sheet
        self._on_stop = on_stop
        self._on_dismiss = on_dismiss

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._drag_start_global: QPoint | None = None
        self._window_start_pos: QPoint | None = None
        self._dragging = False
        self._expanded = False
        self._placed_once = False
        self._phase = "idle"

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        self._pill_frame = QFrame(self)
        self._pill_frame.setStyleSheet(
            f"QFrame {{ background-color: {palette.card_bg}; border: 1px solid {palette.divider}; border-radius: 18px; }}"
        )
        pill_row = QHBoxLayout(self._pill_frame)
        pill_row.setContentsMargins(14, 8, 12, 8)
        pill_row.setSpacing(8)

        self._dot = QLabel("●", self._pill_frame)
        self._dot.setStyleSheet(f"QLabel {{ color: {palette.accent}; font-size: 13px; border: none; }}")
        self._label = QLabel("Agent working…", self._pill_frame)
        self._label.setStyleSheet(
            f"QLabel {{ color: {palette.text_primary}; font-weight: bold; font-size: 13px; border: none; }}"
        )
        self._dismiss_btn = QPushButton("✕", self._pill_frame)
        self._dismiss_btn.setFlat(True)
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss_btn.setStyleSheet(
            f"QPushButton {{ color: {palette.text_secondary}; border: none; font-size: 13px; padding: 0 4px; }}"
        )
        self._dismiss_btn.clicked.connect(self._on_dismiss)
        self._dismiss_btn.setVisible(False)

        pill_row.addWidget(self._dot)
        pill_row.addWidget(self._label)
        pill_row.addWidget(self._dismiss_btn)
        root.addWidget(self._pill_frame, 0, Qt.AlignmentFlag.AlignRight)

        self._actions_frame = QFrame(self)
        self._actions_frame.setStyleSheet(
            f"QFrame {{ background-color: {palette.card_bg}; border: 1px solid {palette.divider}; border-radius: 16px; }}"
        )
        act_row = QHBoxLayout(self._actions_frame)
        act_row.setContentsMargins(10, 5, 10, 5)
        act_row.setSpacing(8)

        self._pause_btn = self._make_chip("Pause", palette.accent, self._on_toggle_pause)
        self._interject_btn = self._make_chip("Interject", palette.accent, self._on_interject)
        self._open_btn = self._make_chip("Open", palette.text_primary, self._on_open_sheet)
        self._stop_btn = self._make_chip("Stop", palette.error, self._on_stop)

        act_row.addWidget(self._pause_btn)
        act_row.addWidget(self._interject_btn)
        act_row.addWidget(self._open_btn)
        act_row.addWidget(self._stop_btn)
        self._actions_frame.setVisible(False)
        root.addWidget(self._actions_frame, 0, Qt.AlignmentFlag.AlignRight)

    def _make_chip(self, text: str, color: str, callback: Callable[[], None]) -> QPushButton:
        btn = QPushButton(text, self._actions_frame)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {color}; font-weight: bold; font-size: 12px; border: none; padding: 4px 6px; }}"
        )
        btn.clicked.connect(callback)
        return btn

    def hwnd(self) -> int:
        return int(self.winId())

    def render_state(self, phase: str, summary: str, sheet_visible: bool) -> None:
        self._phase = phase
        if phase == "idle" or sheet_visible:
            self.hide()
            return
        if phase == "thinking":
            self._dot.setStyleSheet(f"QLabel {{ color: {self._palette.accent}; font-size: 13px; border: none; }}")
            self._label.setText((summary or "Agent working…")[:28])
            self._pause_btn.setText("Pause")
            self._dismiss_btn.setVisible(False)
        elif phase == "paused":
            self._dot.setStyleSheet(f"QLabel {{ color: {self._palette.accent}; font-size: 13px; border: none; }}")
            self._label.setText("Paused")
            self._pause_btn.setText("Resume")
            self._dismiss_btn.setVisible(False)
            self._expanded = True
        elif phase == "confirm":
            self._dot.setStyleSheet(f"QLabel {{ color: {self._palette.record_coral}; font-size: 13px; border: none; }}")
            self._label.setText("Confirm action · click")
            self._dismiss_btn.setVisible(False)
            self._expanded = False
        elif phase == "completed":
            self._dot.setStyleSheet(f"QLabel {{ color: {self._palette.success}; font-size: 13px; border: none; }}")
            self._label.setText("Done · click to view")
            self._dismiss_btn.setVisible(True)
            self._expanded = False
        elif phase == "error":
            self._dot.setStyleSheet(f"QLabel {{ color: {self._palette.error}; font-size: 13px; border: none; }}")
            self._label.setText("Error · click to view")
            self._dismiss_btn.setVisible(True)
            self._expanded = False

        show_actions = self._expanded and phase in ("thinking", "paused")
        self._actions_frame.setVisible(show_actions)
        self.adjustSize()
        if not self._placed_once:
            area = self._work_area_provider(0, 0)
            x, y = compute_initial_pill_position(self.width(), self.height(), area)
            self.move(x, y)
            self._placed_once = True
        if not self.isVisible():
            self.show()
            exclude_hwnd_from_capture(self.hwnd())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_global = event.globalPosition().toPoint()
            self._window_start_pos = self.pos()
            self._dragging = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_start_global is None or self._window_start_pos is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_start_global
        if not self._dragging and (abs(delta.x()) > 6 or abs(delta.y()) > 6):
            self._dragging = True
        if self._dragging:
            self.move(self._window_start_pos + delta)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_dragging = self._dragging
        self._drag_start_global = None
        self._window_start_pos = None
        self._dragging = False
        if was_dragging:
            pos = self.pos()
            area = self._work_area_provider(pos.x(), pos.y())
            sx, sy = snap_pill_to_edge(pos.x(), pos.y(), self.width(), self.height(), area)
            self.move(sx, sy)
        else:
            self._on_pill_clicked()

    def _on_pill_clicked(self) -> None:
        if self._phase in ("confirm", "completed", "error"):
            self._on_open_sheet()
        elif self._phase in ("thinking", "paused"):
            self._expanded = not self._expanded
            self._actions_frame.setVisible(self._expanded)
            self.adjustSize()
