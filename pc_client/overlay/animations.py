"""Window fade/slide helpers and small animated indicator widgets for the PC Jarvis overlay."""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QTimer,
    Qt,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

ANIM_ATTR = "_lw_window_anim"
FRAME_MS = 16


def stop_window_animation(widget: QWidget) -> None:
    """Stops a running fade without firing its completion callback."""
    anim = getattr(widget, ANIM_ATTR, None)
    if anim is not None:
        setattr(widget, ANIM_ATTR, None)
        anim.stop()


def is_window_animating(widget: QWidget) -> bool:
    return getattr(widget, ANIM_ATTR, None) is not None


def _prop(widget: QWidget, name: bytes, start: Any, end: Any, duration_ms: int, curve: QEasingCurve.Type) -> QPropertyAnimation:
    anim = QPropertyAnimation(widget, name)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setDuration(max(1, int(duration_ms)))
    anim.setEasingCurve(curve)
    return anim


def _start_group(widget: QWidget, group: QParallelAnimationGroup, on_finished: Callable[[], None]) -> None:
    def _done() -> None:
        if getattr(widget, ANIM_ATTR, None) is group:
            setattr(widget, ANIM_ATTR, None)
            on_finished()

    group.finished.connect(_done)
    setattr(widget, ANIM_ATTR, group)
    group.start()


def fade_in_window(
    widget: QWidget,
    target: QPoint,
    duration_ms: int = 210,
    slide_px: int = 14,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Shows a top-level window fading in while it slides up into target."""
    stop_window_animation(widget)
    start = QPoint(target.x(), target.y() + slide_px)
    widget.setWindowOpacity(0.0)
    widget.move(start)
    widget.show()
    group = QParallelAnimationGroup(widget)
    group.addAnimation(_prop(widget, b"windowOpacity", 0.0, 1.0, duration_ms, QEasingCurve.Type.OutCubic))
    if slide_px:
        group.addAnimation(_prop(widget, b"pos", start, target, duration_ms, QEasingCurve.Type.OutCubic))

    def _finished() -> None:
        widget.setWindowOpacity(1.0)
        if on_done is not None:
            on_done()

    _start_group(widget, group, _finished)


def fade_out_window(
    widget: QWidget,
    duration_ms: int = 150,
    slide_px: int = 10,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Fades a top-level window out while it slides down, then hides it and restores its position."""
    stop_window_animation(widget)
    if not widget.isVisible():
        widget.setWindowOpacity(1.0)
        if on_done is not None:
            on_done()
        return
    origin = widget.pos()
    group = QParallelAnimationGroup(widget)
    group.addAnimation(
        _prop(widget, b"windowOpacity", widget.windowOpacity(), 0.0, duration_ms, QEasingCurve.Type.InCubic)
    )
    if slide_px:
        end = QPoint(origin.x(), origin.y() + slide_px)
        group.addAnimation(_prop(widget, b"pos", origin, end, duration_ms, QEasingCurve.Type.InCubic))

    def _finished() -> None:
        widget.hide()
        widget.setWindowOpacity(1.0)
        widget.move(origin)
        if on_done is not None:
            on_done()

    _start_group(widget, group, _finished)


class _TimedWidget(QWidget):
    """Base for small widgets that repaint on a frame timer only while visible and active."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._active = False
        self._started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.update)

    def _set_running(self, active: bool) -> None:
        if active and not self._active:
            self._started = time.monotonic()
        self._active = bool(active)
        self._sync_timer()
        self.update()

    def _sync_timer(self) -> None:
        if self._active and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _elapsed(self) -> float:
        return time.monotonic() - self._started

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()


class PulseDot(_TimedWidget):
    """A colored status dot that can emit an expanding, fading ring."""

    PERIOD_S = 1.4

    def __init__(self, color: str, radius: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._radius = max(2, int(radius))
        side = self._radius * 4
        self.setFixedSize(side, side)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_pulsing(self, pulsing: bool) -> None:
        self._set_running(pulsing)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        radius = float(self._radius)
        if self._active:
            phase = (self._elapsed() % self.PERIOD_S) / self.PERIOD_S
            ring = QColor(self._color)
            ring.setAlphaF(max(0.0, 0.45 * (1.0 - phase)))
            ring_radius = radius * (1.0 + phase)
            painter.setBrush(ring)
            painter.drawEllipse(QRectF(cx - ring_radius, cy - ring_radius, ring_radius * 2, ring_radius * 2))
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))


class TypingDots(_TimedWidget):
    """Three softly bouncing dots shown while a reply is on its way."""

    PERIOD_S = 1.1
    PHASE_STEP = 0.18

    def __init__(self, color: str, dot_radius: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._r = max(2, int(dot_radius))
        self._gap = self._r * 1.4
        width = int(self._r * 6 + self._gap * 2) + 2
        self.setFixedSize(width, self._r * 4)

    def set_active(self, active: bool) -> None:
        self.setVisible(bool(active))
        self._set_running(active)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        r = float(self._r)
        base = self._elapsed() / self.PERIOD_S
        for index in range(3):
            t = (base - index * self.PHASE_STEP) % 1.0
            wave = max(0.0, math.sin(t * 2.0 * math.pi))
            color = QColor(self._color)
            color.setAlphaF(0.35 + 0.65 * wave)
            painter.setBrush(color)
            cx = r + 1 + index * (2 * r + self._gap)
            cy = self.height() / 2.0 - wave * r * 0.8
            painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
