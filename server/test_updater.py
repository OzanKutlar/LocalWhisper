"""Unit tests for server/updater.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from updater import (  # noqa: E402
    execute_in_place_update,
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

    def test_execute_in_place_update_writes_online_state_without_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir)
            state_file = repo_dir / ".last_update.json"
            write_update_state(
                {
                    "update_id": "upd999",
                    "phase": "closing",
                    "old_commit": "111aaaa",
                    "host": "127.0.0.1",
                    "port": 9999,
                },
                path=state_file,
            )
            with (
                mock.patch("updater.wait_for_port_free") as wait_mock,
                mock.patch("updater.start_temporary_status_server", return_value=None) as srv_mock,
                mock.patch("updater.run_git_pull", return_value=(True, "Fast-forward", "Fast-forward")),
                mock.patch("updater.get_git_commit", return_value="222bbbb"),
                mock.patch("updater.relaunch_main_server") as relaunch_mock,
            ):
                final_state = execute_in_place_update(
                    repo_dir=repo_dir,
                    state_path=state_file,
                    hold_after_pull_s=0.0,
                )

            wait_mock.assert_called_once_with("127.0.0.1", 9999)
            srv_mock.assert_called_once()
            relaunch_mock.assert_not_called()
            self.assertEqual(final_state["phase"], "online")
            self.assertEqual(final_state["old_commit"], "111aaaa")
            self.assertEqual(final_state["new_commit"], "222bbbb")
            loaded = read_update_state(path=state_file)
            self.assertEqual(loaded["update_id"], "upd999")
            self.assertEqual(loaded["phase"], "online")


if __name__ == "__main__":
    unittest.main()
