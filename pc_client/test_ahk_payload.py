"""Unit tests for ahk_payload. Run: python -m unittest discover -s pc_client -p "test_*.py" -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ahk_payload import (  # noqa: E402
    HUMAN_DELAY_MS,
    TYPING_BUDGET_MS,
    build_send_units,
    plan_typing_delay_ms,
)


def joined(text: str) -> str:
    return "".join(build_send_units(text))


class LiteralTextTests(unittest.TestCase):
    def test_exclamation_is_typed_literally(self):
        self.assertEqual(joined("Wow!"), "Wow{!}")

    def test_plus_hash_and_caret(self):
        self.assertEqual(joined("C++ #tag a^b"), "C{+}{+} {#}tag a{^}b")

    def test_unmatched_braces(self):
        self.assertEqual(joined("{oops"), "{{}oops")
        self.assertEqual(joined("oops}"), "oops{}}")

    def test_newline_tab_and_carriage_return(self):
        self.assertEqual(joined("a\r\nb\tc"), "a{Enter}b{Tab}c")

    def test_non_key_braces_are_literal(self):
        self.assertEqual(joined("{hello world}"), "{{}hello world{}}")

    def test_mode_switches_are_literal(self):
        self.assertEqual(joined("{Raw}"), "{{}Raw{}}")
        self.assertEqual(joined("{^Raw}"), "{{}{^}Raw{}}")


class KeyTokenTests(unittest.TestCase):
    def test_named_keys_pass_through(self):
        self.assertEqual(joined("{Enter}"), "{Enter}")
        self.assertEqual(joined("{Tab 3}"), "{Tab 3}")
        self.assertEqual(joined("{Ctrl down}a{Ctrl up}"), "{Ctrl down}a{Ctrl up}")
        self.assertEqual(joined("{U+1F600}"), "{U+1F600}")

    def test_modifier_combos(self):
        self.assertEqual(joined("{^a}"), "^a")
        self.assertEqual(joined("{^A}"), "^a")
        self.assertEqual(joined("{^+Tab}"), "^+{Tab}")
        self.assertEqual(joined("{!F4}"), "!{F4}")
        self.assertEqual(joined("{#d}"), "#d")

    def test_lone_symbol_types_itself(self):
        self.assertEqual(joined("{!}"), "{!}")

    def test_units_keep_tokens_whole(self):
        self.assertEqual(build_send_units("Hi!{Enter}"), ["H", "i", "{!}", "{Enter}"])


class TypingDelayTests(unittest.TestCase):
    def test_instant_mode(self):
        self.assertEqual(plan_typing_delay_ms(10, human=False), 0)

    def test_short_text_uses_human_pace(self):
        self.assertEqual(plan_typing_delay_ms(10, human=True), HUMAN_DELAY_MS)

    def test_long_text_fits_budget(self):
        self.assertEqual(plan_typing_delay_ms(1000, human=True), TYPING_BUDGET_MS // 1000)
        self.assertEqual(plan_typing_delay_ms(20000, human=True), 0)

    def test_empty(self):
        self.assertEqual(plan_typing_delay_ms(0, human=True), 0)


if __name__ == "__main__":
    unittest.main()
