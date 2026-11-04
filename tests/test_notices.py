"""Notices and status with several sessions per ticket (#7)."""
import contextlib
import io
import json
from unittest import mock

from support import RelayCase, StubServer
from ccrelay import claude_hooks, cli, codexrpc, store

IMPL, VER = "1111111111", "2222222222"


class Notices(RelayCase):
    """One ticket's conversation (thread-1, project = the temp folder) with a verifier report and its draft."""

    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, 'T-1', 'thread-1', ?, 0)",
                        (str(self.root.resolve()), str(self.root)))
        self.ver = self.handoff(session=VER, status="review_ready")
        self.db.execute("UPDATE handoffs SET display_name='verifier' WHERE id=?", (self.ver,))
        self.draft(self.ver)
        self.enterContext(self.codex.installed())
        self.codex.busy.add("thread-1")  # Codex's turn on the verifier's report is running; its menu is shown in it

    def notifications(self):
        path = store.relay_home() / "notifications.log"
        return path.read_text() if path.exists() else ""

    def implementer_reports(self):
        self.impl = self.run_stop_hook(IMPL, role="implementer")["id"]


class QueuedBehindTheMenu(Notices):
    def test_a_report_queued_while_the_menu_is_open_notifies_the_user_without_a_redraw(self):
        srv = StubServer("Keep waiting", while_open=self.implementer_reports)
        self.call(srv, "show_menu", {"handoff": f"H{self.ver}"})
        self.assertIn(f"T-1: implementer report H{self.impl} queued behind the open menu", self.notifications())
        self.assertEqual(len(srv.forms), 1, "the open menu is not redrawn")
        self.assertEqual(len(self.codex.texts), 1, "the report went into Codex's queue, the only queue")
        self.assertIn(f"handoff H{self.impl}", self.codex.texts[0])
        self.assertEqual(store.handoff(self.db, self.impl)["status"], "assessing", "its session stays held")

    def test_no_queued_notice_when_no_menu_is_open(self):
        self.call(StubServer("Keep waiting"), "show_menu", {"handoff": f"H{self.ver}"})  # opened and answered
        self.codex.busy.clear()  # Codex ended that turn
        self.implementer_reports()
        self.assertNotIn("queued behind", self.notifications())


class DiscussWithAReportQueued(Notices):
    def test_discuss_tells_codex_the_queued_report_runs_first(self):
        srv = StubServer("Discuss", while_open=self.implementer_reports)
        result = self.call(srv, "show_menu", {"handoff": f"H{self.ver}"})
        self.assertIn(f"H{self.impl} (implementer) is queued", result)
        self.assertIn("runs first", result)

    def test_discuss_without_a_queued_report_is_as_before(self):
        result = self.call(StubServer("Discuss"), "show_menu", {"handoff": f"H{self.ver}"})
        self.assertNotIn("queued", result)

    def test_menu_text_lists_reports_still_waiting_for_assessment(self):
        self.implementer_reports()
        srv = StubServer("Keep waiting")
        self.call(srv, "show_menu", {"handoff": f"H{self.ver}"})
        self.assertIn(f"Reports waiting for assessment: H{self.impl} from implementer", srv.forms[0]["message"])


class NoticesNameTheSession(Notices):
    def test_review_ready_names_the_session(self):
        self.implementer_reports()
        self.call(StubServer("Keep waiting"), "propose_reply", {"draft": "fix it", "handoff": f"H{self.impl}"})
        self.assertIn(f"T-1: implementer report H{self.impl}: review ready", self.notifications())

    def test_timed_out_names_the_session(self):
        cfg = dict(store.load_config(), hook_wait_secs=0)
        claude_hooks.wait(self.db, cfg, self.ver)
        self.assertIn(f"T-1: verifier report H{self.ver}: timed out", self.notifications())

    def test_assessment_failed_names_the_session(self):
        self.codex.fail = codexrpc.CodexError("daemon down")
        self.implementer_reports()
        self.assertIn(f"T-1: implementer report H{self.impl}: assessment failed", self.notifications())

    def test_session_ended_notification_and_codex_notice_name_the_session(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": VER}))):
            claude_hooks.session_end()
        self.assertIn(f"T-1: verifier report H{self.ver}: Claude session ended", self.notifications())
        self.assertIn("from verifier", self.codex.texts[0].splitlines()[0])


class NoDraftNoticeIsPerHandoff(Notices):
    def test_a_draft_for_another_report_does_not_count(self):
        self.codex.busy.clear()
        self.implementer_reports()  # H{impl} assessed at once, while the verifier's report has a draft
        self.codex.busy.clear()  # Codex ended that turn without drafting for H{impl}
        claude_hooks.check_no_draft(self.db, store.load_config(), store.handoff(self.db, self.impl))
        self.assertIn(f"T-1: implementer report H{self.impl}: assessment finished without a draft",
                      self.notifications())
        self.assertEqual(store.handoff(self.db, self.impl)["status"], "no_draft")


class Status(Notices):
    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.status()
        return {line.split()[0]: line for line in out.getvalue().splitlines() if line.startswith("H")}

    def test_status_shows_each_drafts_source_and_recipient(self):
        self.implementer_reports()
        d = store.latest_draft(self.db, self.ver)["id"]
        self.db.execute("UPDATE drafts SET status='sent', recipient_handoff_id=? WHERE id=?", (self.impl, d))
        rows = self.status()
        self.assertIn("verifier", rows[f"H{self.ver}"])
        self.assertIn(f"D{d}:sent -> implementer H{self.impl}", rows[f"H{self.ver}"])
        self.assertIn("implementer", rows[f"H{self.impl}"])

    def test_an_earlier_draft_sent_elsewhere_keeps_its_recipient_after_a_newer_draft(self):
        self.implementer_reports()
        d3 = store.latest_draft(self.db, self.ver)["id"]
        self.db.execute("UPDATE drafts SET status='sent', recipient_handoff_id=? WHERE id=?", (self.impl, d3))
        d4 = self.draft(self.ver, "a newer draft for the verifier")
        row = self.status()[f"H{self.ver}"]
        self.assertIn(f"D{d4}:ready", row)
        self.assertIn(f"D{d3}:sent -> implementer H{self.impl}", row)

    def test_long_session_names_are_shown_whole(self):
        self.db.execute("UPDATE handoffs SET display_name='cc-codex-relay-dev-222222' WHERE id=?", (self.ver,))
        self.assertIn("cc-codex-relay-dev-222222", self.status()[f"H{self.ver}"])

    def test_a_draft_not_yet_sent_has_no_recipient(self):
        d = store.latest_draft(self.db, self.ver)["id"]
        row = self.status()[f"H{self.ver}"]
        self.assertIn(f"D{d}:ready", row)
        self.assertNotIn("->", row)


class ReviewFixes(Notices):
    """From the #7 review."""

    def test_a_failed_queued_notice_does_not_fail_a_report_already_queued(self):
        def broken_notify(*args, **kw):
            raise OSError("notifications.log is not writable")
        srv = StubServer("Keep waiting", while_open=lambda: self.implementer_reports())
        with mock.patch.object(claude_hooks, "notify", broken_notify):
            self.call(srv, "show_menu", {"handoff": f"H{self.ver}"})
        self.assertEqual(store.handoff(self.db, self.impl)["status"], "assessing", "still held, still queued")

    def test_a_report_codex_is_assessing_now_is_not_called_queued(self):
        self.codex.busy.clear()
        self.implementer_reports()  # Codex starts on it at once: it is being assessed, not queued
        srv = StubServer("Discuss")
        result = self.call(srv, "show_menu", {"handoff": f"H{self.ver}"})  # asked for in that same turn
        self.assertNotIn("queued", result)
        self.assertNotIn("waiting for assessment", srv.forms[0]["message"])

    def test_a_menu_record_left_by_a_turn_that_has_ended_gives_no_false_notice(self):
        store.menu_opened(self.db, "thread-1", self.ver)  # the form was dropped without an answer
        self.codex.busy.clear()  # and Codex's turn has ended
        self.implementer_reports()
        self.assertNotIn("queued behind", self.notifications())
        self.assertIsNone(store.open_menu(self.db, "thread-1"), "the stale record is cleared")

    def test_two_forms_open_on_one_conversation_one_answered_the_other_still_counts(self):
        first = store.menu_opened(self.db, "thread-1", self.ver)
        store.menu_opened(self.db, "thread-1", self.ver)
        store.menu_closed(self.db, first)
        self.implementer_reports()
        self.assertIn("queued behind the open menu", self.notifications())

    def test_toasts_tell_two_sessions_with_the_same_role_apart(self):
        self.db.execute("UPDATE handoffs SET display_name='implementer' WHERE id=?", (self.ver,))
        self.implementer_reports()
        self.call(StubServer("Keep waiting"), "propose_reply", {"draft": "fix it", "handoff": f"H{self.impl}"})
        self.assertIn(f"T-1: implementer ({IMPL[:6]}) report H{self.impl}: review ready", self.notifications())

    def test_queued_behind_the_menu_toast_tells_two_sessions_with_the_same_role_apart(self):
        self.db.execute("UPDATE handoffs SET display_name='implementer' WHERE id=?", (self.ver,))
        self.call(StubServer("Keep waiting", while_open=self.implementer_reports), "show_menu",
                  {"handoff": f"H{self.ver}"})
        self.assertIn(f"T-1: implementer ({IMPL[:6]}) report H{self.impl} queued behind the open menu",
                      self.notifications())
