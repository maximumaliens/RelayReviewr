"""Unit checks for the user's standing context (.scratch/codex-handover.md) riding on hand-offs."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccrelay import intake, store  # noqa: E402

CONTEXT = "The user ruled X on 2026-10-24 because Y."


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def handoff(hid, label="ABC-43 checkout page"):
    return {"id": hid, "created": 0.0, "label": label, "session_id": "abcdef0123456789",
            "claude_message": f"CLAUDE-OUTPUT-{hid}"}


class HandoverOnHandoffs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.old_home = os.environ.get("RELAY_HOME")
        os.environ["RELAY_HOME"] = str(root / "state")
        self.db = store.connect()
        self.repo = root / "Project"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", "seed", cwd=self.repo)
        (self.repo / ".scratch").mkdir()
        self.file = self.repo / ".scratch" / "codex-handover.md"

    def tearDown(self):
        self.db.close()
        if self.old_home is None:
            os.environ.pop("RELAY_HOME", None)
        else:
            os.environ["RELAY_HOME"] = self.old_home
        self.tmp.cleanup()

    def message(self, thread_id, hid, cwd=None):
        """What stop() queues: the hand-off text with any pending handover, then marked as sent."""
        pending = intake.pending_handover(self.db, thread_id, str(cwd or self.repo))
        text = intake.handoff_text(handoff(hid), pending)
        if pending:
            intake.mark_handover_sent(self.db, thread_id, pending["sha256"])
        return text

    def test_first_handoff_carries_the_handover_inside_the_message(self):
        self.file.write_text(CONTEXT)
        text = self.message("T1", 1)
        self.assertTrue(text.startswith("[cc-codex-relay handoff H1 "), "the hand-off header stays first")
        self.assertIn(CONTEXT, text)
        self.assertLess(text.index(CONTEXT), text.index("--- Claude's final output ---"))
        self.assertIn("CLAUDE-OUTPUT-1", text)

    def test_unchanged_handover_is_not_sent_again(self):
        self.file.write_text(CONTEXT)
        self.message("T1", 1)
        self.assertNotIn(CONTEXT, self.message("T1", 2))

    def test_changed_handover_is_sent_again(self):
        self.file.write_text(CONTEXT)
        self.message("T1", 1)
        self.file.write_text(CONTEXT + " Then Z.")
        self.assertIn(CONTEXT + " Then Z.", self.message("T1", 2))

    def test_no_file_gives_the_plain_handoff(self):
        self.assertEqual(self.message("T1", 1), intake.handoff_text(handoff(1)))

    def test_worktree_reads_the_main_checkouts_file(self):
        self.file.write_text(CONTEXT)
        wt = self.repo / ".claude" / "worktrees" / "w"
        git("worktree", "add", "-q", "-b", "w", str(wt), cwd=self.repo)
        self.assertIn(CONTEXT, self.message("T1", 1, cwd=wt))

    def test_each_conversation_gets_its_own_first_copy(self):
        self.file.write_text(CONTEXT)
        self.message("T1", 1)
        self.assertIn(CONTEXT, self.message("T2", 2))

    def test_unreadable_file_gives_the_plain_handoff(self):
        self.file.mkdir()  # a directory where the file should be: unreadable as text
        self.assertEqual(self.message("T1", 1), intake.handoff_text(handoff(1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
