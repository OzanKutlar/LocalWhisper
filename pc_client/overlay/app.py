"""Application coordinator running the Qt Jarvis overlay + background WebSocket client."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from overlay.api import OverlayServerApi
from overlay.hotkey import GlobalHotkeyListener
from overlay.placement import WorkArea, pick_work_area
from overlay.session_store import PcSessionStore
from overlay.settings import OverlaySettings
from overlay.step_aside import StepAsideCoordinator
from overlay.style import FONT_FAMILY, set_ui_scale
from overlay.theme import OverlayPalette, current_palette
from overlay.widgets.pill import CornerPillWindow
from overlay.widgets.sheet import JarvisSheetWindow
from overlay.widgets.toast import PhoneNotificationToastManager
from pc_layout import enumerate_monitors
from pc_tools import set_excluded_hwnds

LOGGER = logging.getLogger("localwhisper.pc_overlay")


class _OverlayBus(QObject):
    summon_requested = Signal()
    hide_for_pc_work = Signal()
    restore_after_pc_work = Signal()
    phone_notification = Signal(str, dict)


def _make_tray_icon(palette: OverlayPalette) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(palette.accent))
    painter.setPen(QColor(palette.accent_on))
    painter.drawRoundedRect(6, 6, 52, 52, 16, 16)
    painter.setBrush(QColor(palette.accent_on))
    painter.drawRoundedRect(26, 16, 12, 22, 6, 6)
    painter.drawRect(30, 40, 4, 8)
    painter.drawRect(22, 47, 20, 3)
    painter.end()
    return QIcon(pix)


def _work_area_at(px: int, py: int) -> WorkArea:
    monitors = enumerate_monitors()
    areas = [WorkArea(m.work_x, m.work_y, m.work_w, m.work_h) for m in monitors if m.work_w > 0 and m.work_h > 0]
    return pick_work_area(px, py, areas)


class OverlayController:
    """Owns the Qt sheet, corner pill, tray icon, global hotkey and step-aside bridge."""

    def __init__(
        self,
        server_http_base: str,
        client_name: str,
        settings: OverlaySettings,
        local_ahk_sender: Callable[[str], None],
    ) -> None:
        self.settings = settings
        self.palette = current_palette()
        self.session_store = PcSessionStore()
        self.api = OverlayServerApi(server_http_base)
        self._client_name = client_name
        self._own_client_id = ""
        self._bus = _OverlayBus()

        self.step_aside = StepAsideCoordinator(
            hide_on_ui=self._hide_from_worker,
            restore_on_ui=self._restore_from_worker,
        )

        self.sheet = JarvisSheetWindow(
            palette=self.palette,
            settings=self.settings,
            session_store=self.session_store,
            api=self.api,
            step_aside=self.step_aside,
            work_area_provider=_work_area_at,
            local_ahk_sender=local_ahk_sender,
            own_client_info_getter=self.own_client_info,
            excluded_hwnds_getter=self.excluded_hwnds,
            on_runtime_phase_changed=self._on_runtime_phase_changed,
        )
        self.pill = CornerPillWindow(
            palette=self.palette,
            work_area_provider=_work_area_at,
            on_toggle_pause=self.sheet.toggle_pause_agent,
            on_interject=lambda: self.sheet.reopen_from_pill(focus_interject=True),
            on_open_sheet=lambda: self.sheet.reopen_from_pill(focus_interject=False),
            on_stop=self.sheet.stop_agent,
            on_dismiss=self.sheet.dismiss_overlay,
        )

        self.toasts = PhoneNotificationToastManager(
            palette=self.palette,
            work_area_provider=_work_area_at,
            reply_fn=self.api.reply_phone_notification,
            dismiss_fn=self.api.dismiss_phone_notification,
            on_hwnds_changed=self._sync_excluded_hwnds,
        )

        self._bus.summon_requested.connect(self.sheet.summon_at_cursor)
        self._bus.hide_for_pc_work.connect(self.sheet.hide_temporarily_for_pc_work)
        self._bus.restore_after_pc_work.connect(self.sheet.restore_after_pc_work)
        self._bus.phone_notification.connect(self.toasts.on_notification_event)

        self._sync_excluded_hwnds()
        self._hotkey: GlobalHotkeyListener | None = None
        self._tray: QSystemTrayIcon | None = None

    def set_own_client_id(self, client_id: str, name: str) -> None:
        self._own_client_id = client_id
        if name:
            self._client_name = name

    def own_client_info(self) -> tuple[str, str]:
        return self._own_client_id, self._client_name

    def on_phone_notification_message(self, event: str, notification: dict[str, object]) -> None:
        self._bus.phone_notification.emit(event, notification)

    def excluded_hwnds(self) -> set[int]:
        return {self.sheet.hwnd(), self.pill.hwnd(), *self.toasts.active_hwnds()}

    def _sync_excluded_hwnds(self) -> None:
        set_excluded_hwnds(self.excluded_hwnds())

    def _hide_from_worker(self) -> None:
        done = threading.Event()

        def _do() -> None:
            self.sheet.hide_temporarily_for_pc_work()
            done.set()

        self._bus.hide_for_pc_work.emit()
        done.wait(timeout=0.08)

    def _restore_from_worker(self) -> None:
        self._bus.restore_after_pc_work.emit()

    def _on_runtime_phase_changed(self, phase: str, summary: str, sheet_visible: bool) -> None:
        self._sync_excluded_hwnds()
        self.pill.render_state(phase, summary, sheet_visible)

    def start_hotkey_and_tray(self, app: QApplication) -> None:
        try:
            self._hotkey = GlobalHotkeyListener(
                self.settings.hotkey,
                on_pressed=lambda: self._bus.summon_requested.emit(),
            )
            self._hotkey.start()
        except ValueError as exc:
            LOGGER.warning("Invalid hotkey '%s': %s", self.settings.hotkey, exc)

        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(_make_tray_icon(self.palette), app)
            tray.setToolTip(f"LocalWhisper ({self.settings.hotkey})")
            menu = QMenu()
            summon_act = QAction(f"Summon Jarvis ({self.settings.hotkey})", menu)
            summon_act.triggered.connect(self.sheet.summon_at_cursor)
            menu.addAction(summon_act)

            kbd_act = QAction("Start in Keyboard Mode", menu)
            kbd_act.setCheckable(True)
            kbd_act.setChecked(self.settings.start_in_keyboard_mode)
            kbd_act.triggered.connect(self._toggle_start_keyboard)
            menu.addAction(kbd_act)

            quit_act = QAction("Quit LocalWhisper PC Client", menu)
            quit_act.triggered.connect(app.quit)
            menu.addSeparator()
            menu.addAction(quit_act)
            tray.setContextMenu(menu)
            tray.activated.connect(
                lambda reason: self.sheet.summon_at_cursor()
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
            tray.show()
            self._tray = tray

    def _toggle_start_keyboard(self, checked: bool) -> None:
        self.settings.start_in_keyboard_mode = bool(checked)
        self.settings.save()

    def shutdown(self) -> None:
        self.step_aside.cancel_restore_timer()
        self.toasts.close_all()
        if self._hotkey is not None:
            self._hotkey.stop()
        if self._tray is not None:
            self._tray.hide()


def run_with_overlay(
    server_url: str,
    server_http_base: str,
    client_name: str,
    ahk_exe: Path,
    send_mode: str,
    human_typing: bool,
    settings: OverlaySettings,
    execute_ahk_send_fn: Callable[[Path, str, str, bool], None],
    ws_runner_fn: Callable[[OverlayController, threading.Event], None],
) -> int:
    """Starts the Qt application on the main thread and runs the WebSocket loop on a daemon thread."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    scale = set_ui_scale(settings.ui_scale)
    app.setFont(QFont(FONT_FAMILY, max(9, int(round(10 * scale)))))
    LOGGER.info("Jarvis popup UI scale: %.2f", scale)

    controller = OverlayController(
        server_http_base=server_http_base,
        client_name=client_name,
        settings=settings,
        local_ahk_sender=lambda text: execute_ahk_send_fn(ahk_exe, text, send_mode, human_typing),
    )
    controller.start_hotkey_and_tray(app)

    stop_event = threading.Event()
    ws_thread = threading.Thread(
        target=ws_runner_fn,
        args=(controller, stop_event),
        name="LocalWhisper-WS",
        daemon=True,
    )
    ws_thread.start()
    LOGGER.info(
        "Jarvis popup ready — press %s to summon below your mouse cursor", settings.hotkey
    )
    try:
        return int(app.exec())
    finally:
        stop_event.set()
        controller.shutdown()
