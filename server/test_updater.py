"""Unit tests for server/updater.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from updater import (  # noqa: E402
    read_update_state,
    summarize_git_output,
    write_update_state,
)


class UpdaterTests(unittest.TestCase):
    def test_summarize_git_output_already_up_to_date(self) -> None:
        self.assertEqual(summarize_git_output("Already up to date.\n"), "Already up to date")

    def test_summarize_git_output_fast_forward(self) -> None:
        sample = "Updating a1b2c3d..e4f5g6h\nFast-forward\n server/main.py | 12 +++++++++---"
        summary = summarize_git_output(sample)
        self.assertIn("Updating a1b2c3d..e4f5g6h", summary)
        self.assertIn("Fast-forward", summary)

    def test_write_and_read_update_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / ".last_update.json"
            state = {
                "update_id": "upd123",
                "phase": "online",
                "ok": True,
                "old_commit": "abc1111",
                "new_commit": "def2222",
                "git_summary": "Fast-forward",
            }
            write_update_state(state, path=state_file)
            loaded = read_update_state(path=state_file)
            self.assertEqual(loaded["update_id"], "upd123")
            self.assertEqual(loaded["new_commit"], "def2222")
            self.assertTrue(loaded["ok"])


if __name__ == "__main__":
    unittest.main()
