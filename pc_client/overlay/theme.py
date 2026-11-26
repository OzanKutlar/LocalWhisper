"""Color palettes matching Android app/src/main/res/values/colors.xml and values-night/colors.xml."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class OverlayPalette:
    is_dark: bool
    bg: str
    card_bg: str
    card_elevated: str
    divider: str
    pill_bg: str
    text_primary: str
    text_secondary: str
    accent: str
    accent_on: str
    accent_container: str
    record_coral: str
    success: str
    error: str
    error_soft: str
    wave_start: str
    wave_mid: str
    wave_end: str
    wave_hot: str


DARK_PALETTE = OverlayPalette(
    is_dark=True,
    bg="#000000",
    card_bg="#17171A",
    card_elevated="#232327",
    divider="#2C2C30",
    pill_bg="#26262B",
    text_primary="#FAFAFA",
    text_secondary="#8E8E93",
    accent="#3E91FF",
    accent_on="#FFFFFF",
    accent_container="#10335C",
    record_coral="#FB4F6C",
    success="#34C77B",
    error="#FF5A5F",
    error_soft="#26FF5A5F",
    wave_start="#22D3EE",
    wave_mid="#5B8CFF",
    wave_end="#B36BFF",
    wave_hot="#FF6FB5",
)

LIGHT_PALETTE = OverlayPalette(
    is_dark=False,
    bg="#F2F2F2",
    card_bg="#FCFCFC",
    card_elevated="#ECECEF",
    divider="#E2E2E5",
    pill_bg="#E6E6EA",
    text_primary="#111111",
    text_secondary="#6F6F73",
    accent="#0072DE",
    accent_on="#FFFFFF",
    accent_container="#D6E8FF",
    record_coral="#F23D5C",
    success="#1E9E5A",
    error="#E0353B",
    error_soft="#1CE0353B",
    wave_start="#00A9C9",
    wave_mid="#2F5BFF",
    wave_end="#8B3DFF",
    wave_hot="#F03C96",
)


def detect_windows_dark_mode() -> bool:
    """Reads Windows AppsUseLightTheme registry value; defaults to dark mode on error."""
    if os.name != "nt":
        return True
    try:
        import winreg  # noqa: PLC0415

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _regtype = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return True


def current_palette(prefer_dark: bool | None = None) -> OverlayPalette:
    dark = detect_windows_dark_mode() if prefer_dark is None else bool(prefer_dark)
    return DARK_PALETTE if dark else LIGHT_PALETTE
