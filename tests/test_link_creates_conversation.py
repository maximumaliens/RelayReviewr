"""Unit checks for #9: /relay-link and /relay-review create the ticket's Codex conversation while the command runs,
not when the session's first forwarded turn ends."""
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from support import RelayCase  # noqa: E402
from ccrelay import cli, codexrpc, store  # noqa: E402

SESSION = "abcdef0123456789"


class LinkCreatesConversation(RelayCase):
    def setUp(self):
        super().setUp()
        self.repo = self.project("Project")

    def link(self, *args, session=SESSION, once=False):
        return self.run_link(session, self.repo, *args, once=once)

    def opened_by_review(self, label):
        """The conversation `relay review "<label>"` would open from the project, or None if it would fail."""
        kind, rows = cli.resolve_review(self.db, label, str(self.repo))
        return rows[0]["thread_id"] if kind == "open" else None

    def test_link_creates_the_conversation_before_the_command_finishes(self):
        self.link("T-1")
        self.assertEqual(self.opened_by_review("T-1"), "thread-1")

    def test_review_once_creates_the_conversation_before_the_command_finishes(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):  # as run_link runs the command
            transcript = cli.transcript_path(str(self.repo), SESSION)
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}\n')
        self.link("T-1", once=True)
        self.assertEqual(self.opened_by_review("T-1"), "thread-1")

    def test_the_reply_says_the_conversation_can_be_opened_now(self):
        self.assertIn('relay review "T 1"', self.link("T", "1"))

    def test_the_hint_comes_first_where_a_shortened_echo_keeps_it(self):
        """Claude is told to echo the reply, but past live runs show it sometimes keeps only the first sentence."""
        self.assertIn('relay review "T-1"', self.link("T-1").split(". ")[0])
        self.assertIn('relay review "T-2"', self.link("T-2", session="other-session-0001").split(". ")[0])

    def test_creating_the_conversation_queues_nothing(self):
        self.link("T-1")
        self.assertEqual(self.codex.texts, [])

    def test_persona_and_the_loaded_guard_apply_as_for_the_stop_hook(self):
        persona = self.root / "persona.md"
        persona.write_text("PERSONA-MARKER")
        (self.root / "state" / "config.json").write_text(f'{{"personas": {{"{self.repo}": "{persona}"}}}}')
        self.link("T-1")
        self.assertEqual(len(self.codex.instructions), 1)
        self.assertTrue(self.codex.instructions[0].startswith("PERSONA-MARKER"))
        self.assertEqual(self.codex.loaded, ["thread-1"])

    def test_first_handoff_reuses_the_conversation_and_carries_standing_context(self):
        (self.repo / ".scratch").mkdir()
        (self.repo / ".scratch" / "codex-handover.md").write_text("STANDING-CONTEXT-MARKER")
        self.link("T-1")
        h = self.run_stop_hook(SESSION, label="T-1", linked_in=self.repo)
        self.assertEqual(h["thread_id"], "thread-1")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)
        self.assertEqual(len(self.codex.texts), 1)
        self.assertIn("STANDING-CONTEXT-MARKER", self.codex.texts[0])

    def test_an_existing_conversation_is_reused(self):
        self.db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, 'T-1', 't-old', ?, 1)",
                        (store.project_of(str(self.repo)), str(self.repo)))
        self.link("T-1", session="other-session-0001")
        self.link("T-1")
        self.assertEqual(self.opened_by_review("T-1"), "t-old")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)

    def test_unreachable_codex_still_links_and_says_the_conversation_comes_later(self):
        self.codex.fail = codexrpc.CodexError("could not start the Codex daemon:\nboom")
        out = self.link("T-1")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM links WHERE session_id=?", (SESSION,)).fetchone())
        self.assertIsNone(self.opened_by_review("T-1"))
        self.assertIn("couldn't be created yet", out)
        self.assertIn("retried when the first turn is forwarded", out)
        self.assertNotIn("relay review", out)
        self.assertEqual(out.count("\n"), 1, "one line: the command file has Claude echo the line verbatim")
        self.codex.fail = None
        h = self.run_stop_hook(SESSION, label="T-1", linked_in=self.repo)
        self.assertEqual(self.opened_by_review("T-1"), h["thread_id"])

    def test_an_unexpected_codex_reply_still_gives_the_plain_fallback(self):
        self.codex.fail = KeyError("thread")
        out = self.link("T-1")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM links WHERE session_id=?", (SESSION,)).fetchone())
        self.assertIn("couldn't be created yet", out)



class TranscriptPath(RelayCase):
    """/relay-review reads the session's transcript where Claude Code keeps it: under CLAUDE_CONFIG_DIR when that
    is set, else ~/.claude."""

    def test_default_is_under_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)  # restored when the patch ends
            self.assertEqual(cli.transcript_path("/p/my repo", "sid"),
                             self.root / ".claude" / "projects" / "-p-my-repo" / "sid.jsonl")

    def test_claude_config_dir_wins(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": "/cfg"}):
            self.assertEqual(cli.transcript_path("/p/my repo", "sid"),
                             pathlib.Path("/cfg/projects/-p-my-repo/sid.jsonl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
