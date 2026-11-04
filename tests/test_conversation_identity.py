"""Unit checks for conversation identity: a ticket's Codex conversation is keyed by project plus label."""
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from support import ROOT, RelayCase, git  # noqa: E402
from ccrelay import cli, codexrpc, intake, store  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE threads (
    label TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created REAL NOT NULL
);
"""


class ConversationIdentity(RelayCase):
    def setUp(self):
        super().setUp()
        self.started = []

        def start_thread(cfg, cwd, instructions, label):
            self.started.append((cwd, label))
            return f"thread-{len(self.started)}"

        for target, repl in (("ensure_daemon", lambda cfg: None), ("ensure_loaded", lambda cfg, t: None),
                             ("start_thread", start_thread)):
            p = mock.patch.object(codexrpc, target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = store.load_config()

    def thread(self, label, cwd):
        return intake.ensure_thread(self.db, self.cfg, label, str(cwd))

    def test_same_label_in_two_projects_gives_two_conversations(self):
        a, b = self.project("A"), self.project("B")
        self.assertNotEqual(self.thread("123", a), self.thread("123", b))

    def test_same_label_in_the_same_project_reuses_the_conversation(self):
        a = self.project("A")
        self.assertEqual(self.thread("123", a), self.thread("123", a))
        self.assertEqual(len(self.started), 1)

    def test_worktree_shares_the_projects_conversation(self):
        a = self.project("A")
        wt = self.root / "elsewhere" / "wt"
        git("worktree", "add", "-q", "-b", "wt", str(wt), cwd=a)
        self.assertEqual(self.thread("123", a), self.thread("123", wt))
        self.assertEqual(len(self.started), 1)

    def test_folder_outside_a_repository_is_its_own_project(self):
        p, q = self.root / "plain1", self.root / "plain2"
        p.mkdir()
        q.mkdir()
        self.assertNotEqual(self.thread("123", p), self.thread("123", q))
        self.assertEqual(self.thread("123", p), self.thread("123", p))

    def test_project_of_a_subfolder_is_the_repository(self):
        a = self.project("A")
        sub = a / "src"
        sub.mkdir()
        self.assertEqual(self.thread("123", a), self.thread("123", sub))

    def test_two_simultaneous_first_handoffs_share_one_conversation(self):
        """Two sessions on one project and label hand off at the same moment, each from its own hook process
        (here a thread with its own connection). One Codex thread is started and both get it."""
        a = self.project("A")
        both_checked = threading.Barrier(2, timeout=5)
        start_thread = codexrpc.start_thread

        def slow_start(cfg, cwd, instructions, label):
            time.sleep(0.5)  # wide enough for the other session's check to run first
            return start_thread(cfg, cwd, instructions, label)

        got, errors = [], []

        def first_handoff():
            try:
                both_checked.wait()
                got.append(intake.ensure_thread(store.connect(), self.cfg, "123", str(a)))
            except Exception as e:
                errors.append(e)

        with mock.patch.object(codexrpc, "start_thread", slow_start):
            workers = [threading.Thread(target=first_handoff) for _ in range(2)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.started), 1, "no orphan Codex thread")
        self.assertEqual(got, ["thread-1", "thread-1"])


class Migration(RelayCase):
    def old_db(self, rows):
        """Replace the fresh database with one in the old schema (label as the primary key)."""
        self.db.close()
        for f in pathlib.Path(os.environ["RELAY_HOME"]).glob("relay.db*"):
            f.unlink()
        db = sqlite3.connect(pathlib.Path(os.environ["RELAY_HOME"]) / "relay.db")
        db.executescript(OLD_SCHEMA)
        db.executemany("INSERT INTO threads VALUES (?, ?, ?, 1.0)", rows)
        db.commit()
        db.close()

    def test_existing_conversations_are_found_again_under_their_project(self):
        a = self.project("A")
        wt = self.root / "wt"
        git("worktree", "add", "-q", "-b", "wt", str(wt), cwd=a)
        plain = self.root / "plain"
        plain.mkdir()
        self.old_db([("one", "t-one", str(a)), ("two", "t-two", str(wt)), ("three", "t-three", str(plain))])
        self.db = store.connect()
        projects = {r["label"]: (r["project"], r["thread_id"]) for r in self.db.execute("SELECT * FROM threads")}
        self.assertEqual(projects, {"one": (str(a.resolve()), "t-one"), "two": (str(a.resolve()), "t-two"),
                                    "three": (str(plain.resolve()), "t-three")})

    def test_migrated_conversation_is_reused_not_recreated(self):
        a = self.project("A")
        self.old_db([("123", "t-old", str(a))])
        self.db = store.connect()
        with mock.patch.object(codexrpc, "ensure_daemon"), mock.patch.object(codexrpc, "ensure_loaded"), \
                mock.patch.object(codexrpc, "start_thread") as start:
            self.assertEqual(intake.ensure_thread(self.db, store.load_config(), "123", str(a)), "t-old")
        start.assert_not_called()

    def test_migration_runs_once_and_allows_the_same_label_in_another_project(self):
        a, b = self.project("A"), self.project("B")
        self.old_db([("123", "t-a", str(a))])
        store.connect().close()
        self.db = store.connect()
        self.db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, '123', 't-b', ?, 1)",
                        (str(b.resolve()), str(b)))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 2)


class ReviewResolution(RelayCase):
    def seed(self, label, project, thread):
        self.db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, ?, ?, ?, 1)",
                        (str(project.resolve()), label, thread, str(project)))

    def test_uses_the_project_containing_the_current_folder(self):
        a, b = self.project("A"), self.project("B")
        self.seed("123", a, "t-a")
        self.seed("123", b, "t-b")
        sub = b / "src"
        sub.mkdir()
        kind, rows = cli.resolve_review(self.db, "123", str(sub))
        self.assertEqual((kind, [r["thread_id"] for r in rows]), ("open", ["t-b"]))

    def test_current_project_wins_from_a_worktree(self):
        a, b = self.project("A"), self.project("B")
        self.seed("123", a, "t-a")
        self.seed("123", b, "t-b")
        wt = self.root / "wt"
        git("worktree", "add", "-q", "-b", "wt", str(wt), cwd=a)
        kind, rows = cli.resolve_review(self.db, "123", str(wt))
        self.assertEqual((kind, [r["thread_id"] for r in rows]), ("open", ["t-a"]))

    def test_opens_the_only_match_from_elsewhere(self):
        a = self.project("A")
        self.seed("123", a, "t-a")
        elsewhere = self.root / "plain"
        elsewhere.mkdir()
        kind, rows = cli.resolve_review(self.db, "123", str(elsewhere))
        self.assertEqual((kind, [r["thread_id"] for r in rows]), ("open", ["t-a"]))

    def test_several_matches_elsewhere_are_listed_not_opened(self):
        a, b = self.project("A"), self.project("B")
        self.seed("123", a, "t-a")
        self.seed("123", b, "t-b")
        elsewhere = self.root / "plain"
        elsewhere.mkdir()
        kind, rows = cli.resolve_review(self.db, "123", str(elsewhere))
        self.assertEqual((kind, len(rows)), ("choose", 2))

    def test_no_match(self):
        kind, rows = cli.resolve_review(self.db, "nope", str(self.root))
        self.assertEqual((kind, rows), ("none", []))

    def test_relay_review_lists_the_projects_and_exits_without_opening(self):
        a, b = self.project("A"), self.project("B")
        self.seed("123", a, "t-a")
        self.seed("123", b, "t-b")
        elsewhere = self.root / "plain"
        elsewhere.mkdir()
        r = subprocess.run([str(ROOT / "bin" / "relay"), "review", "123"], cwd=elsewhere, capture_output=True,
                           text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=""))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(str(a.resolve()), r.stderr)
        self.assertIn(str(b.resolve()), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
