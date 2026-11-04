"""Unit checks for the acceptance run's guard: the installed relay's store must hold no row this run wrote."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "acceptance"))
import isolation  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccrelay import store  # noqa: E402


class StoreGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "relay.db"

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT)")
        return db

    def test_no_change_passes(self):
        db = self.writer()
        db.execute("INSERT INTO events (kind) VALUES ('x')")
        before = isolation.snapshot(self.path)
        self.assertEqual(isolation.changes(before, isolation.snapshot(self.path)), [])
        db.close()

    def test_a_new_row_is_reported_even_before_a_checkpoint(self):
        db = self.writer()  # left open, so the new row is only in the WAL
        before = isolation.snapshot(self.path)
        db.execute("INSERT INTO events (kind) VALUES ('x')")
        self.assertEqual(isolation.changes(before, isolation.snapshot(self.path)),
                         [{"table": "events", "rowid": 1, "change": "new", "before": None,
                           "after": {"id": 1, "kind": "x"}}])
        db.close()

    def test_a_replaced_row_is_reported_although_the_count_is_unchanged(self):
        db = self.writer()
        db.execute("INSERT INTO events (kind) VALUES ('x')")
        before = isolation.snapshot(self.path)
        db.execute("DELETE FROM events")
        db.execute("INSERT INTO events (kind) VALUES ('y')")
        self.assertEqual(isolation.changes(before, isolation.snapshot(self.path)),
                         [{"table": "events", "rowid": 1, "change": "replaced", "before": {"id": 1, "kind": "x"},
                           "after": {"id": 1, "kind": "y"}}])
        db.close()

    def test_a_missing_store_is_not_created(self):
        self.assertIsNone(isolation.snapshot(self.path))
        self.assertFalse(self.path.exists())
        self.assertEqual(isolation.changes(None, None), [])

    def test_a_store_that_appears_is_reported(self):
        before = isolation.snapshot(self.path)
        self.writer().close()
        self.assertEqual([c["change"] for c in isolation.changes(before, isolation.snapshot(self.path))],
                         ["the store was created"])

    def test_snapshot_does_not_write_the_store(self):
        self.writer().close()
        mtime = self.path.stat().st_mtime_ns
        isolation.snapshot(self.path)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)


TEST_SID = "3f2a9c1e-0b7d-4e55-9a1c-8d2e6f4b7a10"
USER_SID = "a81c44d0-5e6f-4b1a-9c3d-2f7e8b9a0c11"
MARKS = {"session_ids": {TEST_SID}, "labels": {"T1-101500"},
         "roots": [pathlib.Path("/tmp/relay-accept"), pathlib.Path("/home/u/.local/state/cc-codex-relay-accept")]}


class Attribution(unittest.TestCase):
    """Only rows carrying this run's session IDs, labels or acceptance paths, directly or through their hand-off
    or thread, make the run invalid. Message text never does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "relay.db"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(store.SCHEMA)  # the relay's real tables, written here without ccrelay.store.connect
        self.before = isolation.snapshot(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def split(self):
        return isolation.attribute(self.before, isolation.snapshot(self.path), MARKS)

    def tables(self, changed):
        return [(c["table"], c["change"]) for c in changed]

    def handoff(self, sid, cwd="/home/u/my-project", label="ABC-42 triage", message="done", thread="th-user"):
        return self.db.execute("INSERT INTO handoffs (session_id, label, thread_id, cwd, claude_message, status,"
                               " created, updated) VALUES (?, ?, ?, ?, ?, 'queued', 0, 0)",
                               (sid, label, thread, cwd, message)).lastrowid

    def test_a_row_with_a_test_session_id_is_attributed(self):
        self.db.execute("INSERT INTO sessions VALUES (?, 1.5)", (TEST_SID,))
        attributed, other = self.split()
        self.assertEqual(attributed, [{"table": "sessions", "rowid": 1, "change": "new", "before": None,
                                       "after": {"session_id": TEST_SID, "ended": 1.5}}])
        self.assertEqual(other, [])

    def test_a_row_with_a_test_label_is_attributed(self):
        self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'T1-101500', '/home/u/p', 0)",
                        (USER_SID,))
        self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (0, 'link', ?)", (f"{USER_SID} -> T1-101500",))
        attributed, other = self.split()
        self.assertEqual(self.tables(attributed), [("events", "new"), ("links", "new")])
        self.assertEqual(other, [])

    def test_a_link_without_its_conversation_yet_is_attributed_by_its_session(self):
        self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (0, 'link:no_conversation', ?)",
                        (f"{TEST_SID} -> ABC-42 triage",))
        attributed, other = self.split()
        self.assertEqual(self.tables(attributed), [("events", "new")])
        self.assertEqual(other, [])

    def test_a_row_with_a_path_under_an_acceptance_root_is_attributed(self):
        self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'ABC-42 triage',"
                        " '/tmp/relay-accept/repo', 0)", (USER_SID,))
        attributed, other = self.split()
        self.assertEqual(self.tables(attributed), [("links", "new")])
        self.assertEqual(other, [])

    def test_a_path_that_only_shares_a_roots_prefix_is_not_attributed(self):
        self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'ABC-42 triage',"
                        " '/tmp/relay-accepted/repo', 0)", (USER_SID,))
        attributed, other = self.split()
        self.assertEqual(attributed, [])
        self.assertEqual(self.tables(other), [("links", "new")])

    def test_a_row_from_the_users_own_session_is_listed_but_not_attributed(self):
        self.db.execute("INSERT INTO sessions VALUES (?, 2.0)", (USER_SID,))
        self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'ABC-42 triage',"
                        " '/home/u/my-project', 0)", (USER_SID,))
        self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (0, 'link', ?)", (f"{USER_SID} -> ABC-42 triage",))
        attributed, other = self.split()
        self.assertEqual(attributed, [])
        self.assertEqual(self.tables(other), [("events", "new"), ("links", "new"), ("sessions", "new")])

    def test_message_text_that_mentions_the_run_is_not_attributed(self):
        hid = self.handoff(USER_SID, message=f"Test {TEST_SID} on T1-101500 left /tmp/relay-accept/repo dirty")
        self.db.execute("INSERT INTO events (ts, handoff_id, kind, detail) VALUES (0, ?, 'notify', ?)",
                        (hid, "Codex: T1-101500 under /tmp/relay-accept/repo looks fine"))
        attributed, other = self.split()
        self.assertEqual(attributed, [])
        self.assertEqual(self.tables(other), [("events", "new"), ("handoffs", "new")])

    def test_rows_linked_to_a_test_hand_off_or_thread_are_attributed(self):
        hid = self.handoff(TEST_SID, thread="th-test")
        self.before = isolation.snapshot(self.path)  # the test hand-off leaked earlier; its dependants appear now
        self.db.execute("INSERT INTO drafts (handoff_id, text, path, status, created, updated)"
                        " VALUES (?, 'reply', '/home/u/drafts/D1.md', 'ready', 0, 0)", (hid,))
        self.db.execute("INSERT INTO events (ts, handoff_id, kind, detail) VALUES (0, ?, 'notify', 'Codex: ready')",
                        (hid,))
        self.db.execute("INSERT INTO handovers_sent VALUES ('th-test', 'abc', 0)")
        attributed, other = self.split()
        self.assertEqual(self.tables(attributed), [("drafts", "new"), ("events", "new"), ("handovers_sent", "new")])
        self.assertEqual(other, [])

    def test_a_removed_test_row_is_attributed_by_what_it_held(self):
        self.db.execute("INSERT INTO sessions VALUES (?, 1.0)", (TEST_SID,))
        self.before = isolation.snapshot(self.path)
        self.db.execute("DELETE FROM sessions")
        attributed, other = self.split()
        self.assertEqual(self.tables(attributed), [("sessions", "removed")])
        self.assertEqual(other, [])

    def test_a_replaced_row_is_attributed_by_what_it_held_before_or_after(self):
        self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'T1-101500', '/home/u/p', 0)",
                        (USER_SID,))
        self.db.execute("INSERT INTO sessions VALUES (?, 1.0)", (USER_SID,))
        self.before = isolation.snapshot(self.path)
        self.db.execute("UPDATE links SET label='ABC-42 triage'")
        self.db.execute("UPDATE sessions SET ended=2.0")
        attributed, other = self.split()
        self.assertEqual([(c["table"], c["change"], c["before"]["label"], c["after"]["label"]) for c in attributed],
                         [("links", "replaced", "T1-101500", "ABC-42 triage")])
        self.assertEqual([(c["table"], c["change"], c["before"]["ended"], c["after"]["ended"]) for c in other],
                         [("sessions", "replaced", 1.0, 2.0)])

    def test_no_change_attributes_nothing(self):
        self.db.execute("INSERT INTO sessions VALUES (?, 1.0)", (TEST_SID,))
        self.before = isolation.snapshot(self.path)
        self.assertEqual(self.split(), ([], []))


class Guard(unittest.TestCase):
    """The summary's opening section and run.py's exit code."""
    MINE = {"table": "sessions", "rowid": 7, "change": "new", "before": None,
            "after": {"session_id": TEST_SID, "ended": 1.5}}
    OTHER = {"table": "links", "rowid": 3, "change": "replaced", "before": {"label": "ABC-42 triage", "once": 0},
             "after": {"label": "ABC-42 triage", "once": 1}}

    def test_a_row_this_run_wrote_makes_the_run_invalid_and_is_shown(self):
        text, code = isolation.guard([self.MINE], [self.OTHER], "/s/relay.db")
        self.assertEqual(code, 1)
        mine, other = text.split("Not attributed to this run")
        self.assertTrue(mine.startswith("**INVALID run"))
        self.assertIn("sessions row 7, new: {'session_id': '3f2a9c1e-0b7d-4e55-9a1c-8d2e6f4b7a10', 'ended': 1.5}", mine)
        self.assertIn("links row 3, replaced: {'label': 'ABC-42 triage', 'once': 0} -> {'label': 'ABC-42 triage', 'once': 1}",
                      other)

    def test_rows_not_attributed_are_listed_and_the_run_stays_valid(self):
        text, code = isolation.guard([], [self.OTHER], "/s/relay.db")
        self.assertEqual(code, 0)
        self.assertNotIn("INVALID", text)
        self.assertIn("Not attributed to this run", text)
        self.assertIn("links row 3, replaced", text)

    def test_an_unchanged_store_is_valid(self):
        text, code = isolation.guard([], [], "/s/relay.db")
        self.assertEqual(code, 0)
        self.assertEqual(text, "Isolation check: the installed relay's store (/s/relay.db) is unchanged.\n")

    def test_long_values_are_shortened_in_the_summary(self):
        long = {**self.OTHER, "after": {"label": "x" * 500, "once": 1}}
        text, _ = isolation.guard([], [long], "/s/relay.db")
        self.assertNotIn("x" * 201, text)


ACCEPTANCE = pathlib.Path(__file__).resolve().parent / "acceptance"
MARKS_PROBE = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import harness  # clears this process's environment, so it runs in a child
from harness import setup, store
db = store.connect()  # the development store, under the temporary acceptance root
db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES ('sid-linked', 'T1-101500', '/x', 0)")
db.execute("INSERT INTO handoffs (session_id, label, cwd, claude_message, status, created, updated)"
           " VALUES ('sid-linked', 'T1-101500', '/x', 'm', 'queued', 0, 0)")
db.execute("INSERT INTO sessions VALUES ('sid-ended', 1)")
db.close()
projects = setup.CLAUDE_CONFIG_DIR / "projects" / "-tmp-relay-accept-repo"
(projects / "sid-transcript-only" / "subagents").mkdir(parents=True)
(projects / "sid-transcript-only.jsonl").write_text("{}\\n")
(projects / "sid-transcript-only" / "subagents" / "agent-1.jsonl").write_text("{}\\n")
(projects / "sid-earlier-run.jsonl").write_text("{}\\n")
os.utime(projects / "sid-earlier-run.jsonl", (1, 1))
sids, labels = harness.marks(time.time() - 60)
print(json.dumps({"sids": sorted(sids), "labels": sorted(labels)}))
"""


class Runner(unittest.TestCase):
    """run.py's exit status and the marks the harness collects. Each runs in a child process whose acceptance roots
    are a temporary folder, so neither the development nor the installed relay is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "RELAY_ACCEPT_ROOT": f"{self.tmp.name}/accept",
                    "RELAY_ACCEPT_TMP": f"{self.tmp.name}/tmp"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_runner_error_exits_2_with_a_diagnostic_not_1(self):
        r = subprocess.run([sys.executable, str(ACCEPTANCE / "run.py"), "not-a-test-number"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("ValueError", r.stderr)
        self.assertIn("not an INVALID run", r.stderr)

    def test_marks_include_a_test_session_found_only_in_a_transcript(self):
        r = subprocess.run([sys.executable, "-c", MARKS_PROBE, str(ACCEPTANCE)], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"sids": ["sid-ended", "sid-linked", "sid-transcript-only"],
                                                "labels": ["T1-101500"]})


class RefuseInstalledPaths(unittest.TestCase):
    def test_installed_paths_are_refused(self):
        home = pathlib.Path.home()
        for p in (home / ".local/state/cc-codex-relay", home / ".codex-reviewer", home / ".codex", home / ".claude"):
            with self.assertRaises(SystemExit):
                isolation.refuse_installed(p)

    def test_other_paths_pass(self):
        isolation.refuse_installed(pathlib.Path(tempfile.gettempdir()) / "relay-accept" / "relay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
