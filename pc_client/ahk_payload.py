"""Converts dictated text into AutoHotkey v1.1 Send units.

Only {...} tokens that look like real AutoHotkey key syntax are sent as keys.
Everything else is typed literally, so punctuation such as ! + ^ # can never
trigger Alt, Shift, Ctrl or Win.

Inside braces, AutoHotkey syntax is assumed:
    {Enter} {Tab 3} {Ctrl down} {U+1F600}   key names, repeats, down/up, unicode
    {^a} {^+Tab} {!F4} {#d}                 modifier combos (Ctrl+A, Ctrl+Shift+Tab, ...)
    {!} {+} {^}                             a lone symbol types that symbol
"""

from __future__ import annotations

import re

HUMAN_DELAY_MS = 50          # average pause per unit when typing at human pace
TYPING_BUDGET_MS = 8000      # long messages speed up so typing stays within this budget
MAX_TOKEN_LENGTH = 40

BRACE_CANDIDATE = re.compile(r"\{([^{}\r\n]{1,%d})\}" % MAX_TOKEN_LENGTH)
KEY_NAME = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_]*|[0-9]|U\+[0-9A-Fa-f]{2,6})"
    r"(?: (?:[0-9]{1,3}|down|up|downR|downTemp))?",
    re.IGNORECASE,
)
MODIFIER_COMBO = re.compile(r"([\^!+#]+)(.+)")

# AutoHotkey Send mode switches that would change how the rest of the text is sent.
BLOCKED_NAMES = frozenset({"raw", "text", "blind"})
LITERAL_ESCAPES = {"!": "{!}", "^": "{^}", "+": "{+}", "#": "{#}", "{": "{{}", "}": "{}}"}
CONTROL_KEYS = {"\n": "{Enter}", "\t": "{Tab}"}


def build_send_units(text: str) -> list[str]:
    """Splits text into AutoHotkey Send strings: one per literal character or key token."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    units: list[str] = []
    position = 0
    for match in BRACE_CANDIDATE.finditer(text):
        token = to_ahk_token(match.group(1))
        if token is None:
            continue  # not a key: stays in the literal span and gets escaped
        units.extend(literal_units(text[position:match.start()]))
        units.append(token)
        position = match.end()
    units.extend(literal_units(text[position:]))
    return units


def plan_typing_delay_ms(unit_count: int, human: bool) -> int:
    """Base per-unit delay; 0 means send everything in one burst."""
    if not human or unit_count <= 0:
        return 0
    return min(HUMAN_DELAY_MS, TYPING_BUDGET_MS // unit_count)


def literal_units(segment: str) -> list[str]:
    units: list[str] = []
    for char in segment:
        if char == "\r":
            continue
        units.append(CONTROL_KEYS.get(char) or LITERAL_ESCAPES.get(char) or char)
    return units


def to_ahk_token(content: str) -> str | None:
    """AutoHotkey Send form of a brace token's content, or None when it is not key syntax."""
    if len(content) == 1 and not content.isalnum() and not content.isspace():
        return "{" + content + "}"
    combo = MODIFIER_COMBO.fullmatch(content)
    if combo:
        return combo_token(combo.group(1), combo.group(2))
    if is_key_name(content):
        return "{" + content + "}"
    return None


def combo_token(modifiers: str, key: str) -> str | None:
    if len(key) == 1 and not key.isspace():
        # Lowercase letters: in AutoHotkey ^A would mean Ctrl+Shift+A.
        return modifiers + (key.lower() if key.isalpha() else "{" + key + "}")
    if is_key_name(key):
        return modifiers + "{" + key + "}"
    return None


def is_key_name(content: str) -> bool:
    name = content.split(" ", 1)[0]
    return name.lower() not in BLOCKED_NAMES and KEY_NAME.fullmatch(content) is not None
