"""Qt WaveformWidget ported from Android WaveformView.kt."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

from overlay.theme import OverlayPalette

MAX_BARS = 96
SAMPLE_INTERVAL_S = 0.05
BAR_WIDTH = 3.5
BAR_GAP = 3.0
SLOT_PX = BAR_WIDTH + BAR_GAP
MAX_BAR_FRACTION = 0.90
FADE_EXPONENT = 1.6
SHRINK_AMOUNT = 0.18
DOT_ALPHA = 0.45

DB_FLOOR = -50.0
DB_CEILING = -6.0
MIN_LINEAR_LEVEL = 1e-5

NEWEST_STIFFNESS = 900.0
NEWEST_DAMPING = 27.0
SETTLE_STIFFNESS = 500.0
SETTLE_DAMPING = 45.0


def _to_display_level(raw: float) -> float:
    db = 20.0 * math.log10(max(float(raw), MIN_LINEAR_LEVEL))
    return max(0.0, min(1.0, (db - DB_FLOOR) / (DB_CEILING - DB_FLOOR)))


class WaveformWidget(QWidget):
    """Recorder-style scrolling waveform with coral playhead and gradient bars."""

    def __init__(self, palette: OverlayPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.setFixedHeight(96)
        self._targets = [0.0] * MAX_BARS
        self._heights = [0.0] * MAX_BARS
        self._velocities = [0.0] * MAX_BARS
        self._head = 0
        self._count = 0
        self._last_sample_at = time.monotonic()
        self._last_frame_at = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_frame)

    def set_active(self, active: bool) -> None:
        if active and not self._timer.isActive():
            self._last_frame_at = time.monotonic()
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()

    def reset(self) -> None:
        for idx in range(MAX_BARS):
            self._targets[idx] = 0.0
            self._heights[idx] = 0.0
            self._velocities[idx] = 0.0
        self._head = 0
        self._count = 0
        self._last_sample_at = time.monotonic()
        self.update()

    def push_amplitude(self, raw_level: float) -> None:
        level = _to_display_level(max(0.0, min(1.0, float(raw_level))))
        self._head = (self._head + 1) % MAX_BARS
        self._targets[self._head] = level
        self._heights[self._head] = 0.0
        self._velocities[self._head] = 0.0
        if self._count < MAX_BARS:
            self._count += 1
        self._last_sample_at = time.monotonic()

    def _index_for_age(self, age: int) -> int:
        return (self._head - age + MAX_BARS) % MAX_BARS

    def _on_frame(self) -> None:
        now = time.monotonic()
        dt = max(0.0, min(now - self._last_frame_at, 0.033))
        self._last_frame_at = now
        for age in range(self._count):
            idx = self._index_for_age(age)
            stiff = NEWEST_STIFFNESS if age == 0 else SETTLE_STIFFNESS
            damp = NEWEST_DAMPING if age == 0 else SETTLE_DAMPING
            accel = stiff * (self._targets[idx] - self._heights[idx]) - damp * self._velocities[idx]
            self._velocities[idx] += accel * dt
            self._heights[idx] = max(-0.2, min(1.2, self._heights[idx] + self._velocities[idx] * dt))
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        w = float(self.width())
        h = float(self.height())
        if w <= 0 or h <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        center_y = h / 2.0
        playhead_x = w / 2.0
        half_w = w / 2.0
        fraction = 0.0
        if self._count > 0:
            fraction = max(0.0, min(1.0, (time.monotonic() - self._last_sample_at) / SAMPLE_INTERVAL_S))

        gradient = QLinearGradient(0.0, 0.0, playhead_x, 0.0)
        gradient.setColorAt(0.0, QColor(self._palette.wave_start))
        gradient.setColorAt(0.38, QColor(self._palette.wave_mid))
        gradient.setColorAt(0.72, QColor(self._palette.wave_end))
        gradient.setColorAt(1.0, QColor(self._palette.wave_hot))

        visible = min(self._count, int(half_w / SLOT_PX) + 2)
        max_bar_h = h * MAX_BAR_FRACTION
        radius = BAR_WIDTH / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        for age in range(visible):
            cx = playhead_x - (age + fraction + 0.5) * SLOT_PX
            dist = (playhead_x - cx) / half_w
            if dist >= 1.0:
                break
            fade = 1.0 - math.pow(max(0.0, dist), FADE_EXPONENT)
            scale = 1.0 - SHRINK_AMOUNT * dist
            bar_h = max(BAR_WIDTH, min(h, self._heights[self._index_for_age(age)] * scale * max_bar_h))
            painter.setOpacity(max(0.0, min(1.0, fade)))
            painter.setBrush(gradient)
            rect = QRectF(cx - radius, center_y - bar_h / 2.0, BAR_WIDTH, bar_h)
            painter.drawRoundedRect(rect, radius, radius)

        dot_color = QColor(self._palette.text_secondary)
        dot_count = min(int(half_w / SLOT_PX) + 2, MAX_BARS)
        for idx in range(dot_count):
            cx = playhead_x + (idx + 1.5 - fraction) * SLOT_PX
            dist = (cx - playhead_x) / half_w
            if dist >= 1.0:
                break
            fade = 1.0 - math.pow(max(0.0, dist), FADE_EXPONENT)
            painter.setOpacity(max(0.0, min(1.0, fade * DOT_ALPHA)))
            painter.setBrush(dot_color)
            painter.drawEllipse(QRectF(cx - radius, center_y - radius, BAR_WIDTH, BAR_WIDTH))

        coral = QColor(self._palette.record_coral)
        top_y = 4.0
        bot_y = h - 4.0
        painter.setOpacity(0.28)
        glow_pen = QPen(coral, 7.0)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(glow_pen)
        painter.drawLine(QRectF(playhead_x, top_y, 0.0, bot_y - top_y).topLeft(), QRectF(playhead_x, top_y, 0.0, bot_y - top_y).bottomLeft())

        painter.setOpacity(1.0)
        line_pen = QPen(coral, 2.0)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(line_pen)
        painter.drawLine(QRectF(playhead_x, top_y, 0.0, bot_y - top_y).topLeft(), QRectF(playhead_x, top_y, 0.0, bot_y - top_y).bottomLeft())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(coral)
        painter.drawEllipse(QRectF(playhead_x - 3.5, top_y - 3.5, 7.0, 7.0))
        painter.drawEllipse(QRectF(playhead_x - 3.5, bot_y - 3.5, 7.0, 7.0))
