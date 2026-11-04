"""Session roles (`--as`) and the context each hand-off carries to Codex."""
import io
import json
import os
import subprocess
from unittest import mock

import support
from support import RelayCase, git
from ccrelay import claude_hooks, cli, codexrpc, intake, store

SID = "abcdef0123456789"
RELAY = str(support.ROOT / "bin" / "relay")


class ParseRole(RelayCase):
    def test_role_is_read_before_the_label_is_joined(self):
        self.assertEqual(cli.parse_role(["--as", "verifier", "ABC-42", "triage"]), ("verifier", ["ABC-42", "triage"]))

    def test_role_after_the_label_and_without_a_label(self):
        self.assertEqual(cli.parse_role(["ABC-42", "triage", "--as", "verifier"]), ("verifier", ["ABC-42", "triage"]))
        self.assertEqual(cli.parse_role(["--as", "verifier"]), ("verifier", []))

    def test_no_role(self):
        self.assertEqual(cli.parse_role(["ABC-42", "triage"]), (None, ["ABC-42", "triage"]))
        self.assertEqual(cli.parse_role([]), (None, []))

    def test_as_without_a_role_is_an_error(self):
        with self.assertRaises(SystemExit):
            cli.parse_role(["ABC-42", "--as"])
        with self.assertRaises(SystemExit):
            cli.parse_role(["--as", "--as", "x"])


class Link(RelayCase):
    def link(self, args, cwd):
        self.run_link(SID, cwd, *args)
        return self.db.execute("SELECT * FROM links WHERE session_id=?", (SID,)).fetchone()

    def test_link_keeps_the_role_and_the_whole_spaced_label(self):
        row = self.link(["--as", "verifier", "ABC-42", "triage"], self.project())
        self.assertEqual((row["label"], row["role"]), ("ABC-42 triage", "verifier"))

    def test_link_without_a_role(self):
        row = self.link(["ABC-42", "triage"], self.project())
        self.assertEqual((row["label"], row["role"]), ("ABC-42 triage", None))

    def test_relinking_without_a_role_clears_the_old_one(self):
        repo = self.project()
        self.link(["--as", "verifier", "T-1"], repo)
        self.assertIsNone(self.link(["T-1"], repo)["role"])


class DisplayName(RelayCase):
    def test_role_is_the_name(self):
        self.assertEqual(store.display_name("verifier", "/x/Project", SID), "verifier")

    def test_fallback_is_folder_name_plus_short_session_id(self):
        self.assertEqual(store.display_name(None, "/x/Project", SID), "Project-abcdef")


class FiledHandoff(RelayCase):
    def file(self, repo, role=None, now_in=None):
        link = {"label": "T-1", "cwd": str(repo), "role": role}
        hid = claude_hooks.file_handoff(self.db, SID, link, "report", "/t.jsonl", str(now_in or repo))
        return store.handoff(self.db, hid)

    def test_folder_branch_and_head_come_from_the_folder_the_session_is_in_now(self):
        main = self.project("Proj")
        wt = self.root / "Proj-wt"
        git("worktree", "add", "-q", "-b", "wt-branch", str(wt), cwd=main)
        git("commit", "-q", "--allow-empty", "-m", "worktree work", cwd=wt)
        wt_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=wt, capture_output=True,
                                 text=True).stdout.strip()
        h = self.file(main, now_in=wt)  # linked in the main checkout, reporting from the worktree
        self.assertEqual((h["cwd"], h["branch"], h["head"]), (str(wt), "wt-branch", wt_head))

    def test_records_name_folder_branch_head_and_time(self):
        repo = self.project("Proj")
        git("checkout", "-q", "-b", "feature-x", cwd=repo)
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
                              text=True).stdout.strip()
        before = store.now()
        h = self.file(repo, role="verifier")
        self.assertEqual((h["display_name"], h["cwd"], h["branch"], h["head"]),
                         ("verifier", str(repo), "feature-x", head))
        self.assertGreaterEqual(h["created"], before)

    def test_fallback_name_when_there_is_no_role(self):
        self.assertEqual(self.file(self.project("Proj"))["display_name"], "Proj-abcdef")

    def test_folder_that_is_not_a_repository_still_files(self):
        plain = self.root / "plain"
        plain.mkdir()
        h = self.file(plain)
        self.assertEqual((h["branch"], h["head"]), (None, None))


class StopHookFolder(RelayCase):
    """The Stop hook: the conversation belongs to the project the session was linked in, while the hand-off
    records the folder the session is in when the hook fires."""

    def setUp(self):
        super().setUp()
        self.started, self.queued = [], []

        def start_thread(cfg, cwd, instructions, label):
            self.started.append(cwd)
            return f"thread-{len(self.started)}"

        for target, repl in (("ensure_daemon", lambda cfg: None), ("ensure_loaded", lambda cfg, t: None),
                             ("start_thread", start_thread),
                             ("queue_message", lambda cfg, t, text, cid: self.queued.append(text) or "q1")):
            p = mock.patch.object(codexrpc, target, repl)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(claude_hooks, "wait", lambda db, cfg, hid: 0)
        p.start()
        self.addCleanup(p.stop)

    def stop(self, linked_in, **payload):
        store.connect().execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'T-1', ?, 0)",
                                (SID, str(linked_in)))
        payload = {"session_id": SID, "transcript_path": "/t.jsonl", "last_assistant_message": "report", **payload}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            claude_hooks.stop()
        return self.db.execute("SELECT * FROM handoffs ORDER BY id DESC LIMIT 1").fetchone()

    def test_stopping_outside_the_linked_project_keeps_the_projects_conversation(self):
        proj = self.project("Proj")
        (proj / ".scratch").mkdir()
        (proj / ".scratch" / "codex-handover.md").write_text("STANDING-CONTEXT")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        h = self.stop(proj, cwd=str(elsewhere))
        self.assertEqual(self.started, [str(proj)], "the conversation is started for the linked project")
        self.assertEqual(self.db.execute("SELECT project FROM threads").fetchone()["project"], str(proj.resolve()))
        self.assertIn("STANDING-CONTEXT", self.queued[0], "standing context comes from the linked project")
        self.assertEqual(h["cwd"], str(elsewhere), "the hand-off still records where the session is now")

    def test_linked_folder_that_no_longer_exists_falls_back_to_the_folder_at_stop(self):
        proj = self.project("Proj")
        gone = self.root / "deleted-worktree"
        h = self.stop(gone, cwd=str(proj))
        self.assertEqual(self.started, [str(proj)])
        self.assertEqual(h["status"], "assessing", "the hand-off reached Codex")

    def test_payload_without_a_folder_falls_back_to_the_linked_one(self):
        proj = self.project("Proj")
        self.assertEqual(self.stop(proj)["cwd"], str(proj))


class CodexHeader(RelayCase):
    def text(self, **cols):
        hid = self.handoff(cwd=self.root / "Proj")
        for k, v in cols.items():
            self.db.execute(f"UPDATE handoffs SET {k}=? WHERE id=?", (v, hid))
        return intake.handoff_text(store.handoff(self.db, hid))

    def test_header_shows_who_sent_it_and_where(self):
        text = self.text(display_name="verifier", branch="feature-x", head="1a2b3c4")
        header = text.split("\n\n")[0]
        for needle in ("verifier", str(self.root / "Proj"), "feature-x", "1a2b3c4"):
            self.assertIn(needle, header)

    def test_header_for_a_handoff_without_context_still_renders(self):
        self.assertIn("handoff H", self.text().split("\n\n")[0])


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
