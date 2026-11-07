"""A report another process settled first is never overwritten by the Claude hooks (CODING_STANDARDS:
conditional status changes). Each test settles the report from a second connection, as the menu server or
another hook would, in the moment between the hook reading the report and changing it."""
import contextlib
import io
import json
import unittest
from unittest import mock

from support import RelayCase
from ccrelay import claude_hooks, codexrpc, intake, store

SID = "abcdef0123456789"


class SettledReports(RelayCase):
    @contextlib.contextmanager
    def settled_first(self, status, by, hid=None):
        """Just before the hook moves a report (`hid`, or any) to `status`, another process moves it to `by`."""
        real = store.set_handoff

        def racing(db, h, new, *args, **kwargs):
            if new == status and hid in (None, h):
                self.db.execute("UPDATE handoffs SET status=? WHERE id=?", (by, h))
            return real(db, h, new, *args, **kwargs)
        with mock.patch.object(store, "set_handoff", racing):
            yield

    def notifications(self):
        path = store.relay_home() / "notifications.log"
        return path.read_text() if path.exists() else ""

    def test_a_newer_turn_does_not_supersede_a_report_that_was_sent_meanwhile(self):
        old = self.handoff(session=SID, status="review_ready")
        with self.settled_first("superseded", "sent", old):  # e.g. the menu server confirmed an interrupted send
            new = self.run_stop_hook(SID)
        self.assertEqual(store.handoff(self.db, old)["status"], "sent")
        self.assertEqual(new["status"], "assessing")

    def test_a_newer_turn_still_supersedes_a_waiting_report(self):
        old = self.handoff(session=SID, status="review_ready")
        self.run_stop_hook(SID)
        self.assertEqual(store.handoff(self.db, old)["status"], "superseded")

    def test_a_failed_assessment_start_does_not_overwrite_a_report_settled_meanwhile(self):
        self.codex.fail = codexrpc.CodexError("daemon down")
        with self.settled_first("failed", "session_ended"):  # the session ended while Codex was being reached
            h = self.run_stop_hook(SID)
        self.assertEqual(h["status"], "session_ended")

    def test_a_failed_assessment_start_still_marks_a_queued_report_failed(self):
        self.codex.fail = codexrpc.CodexError("daemon down")
        h = self.run_stop_hook(SID)
        self.assertEqual(h["status"], "failed")
        self.assertIn("daemon down", h["reason"])

    def end_session(self):
        with self.codex.installed(), mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": SID}))):
            claude_hooks.session_end()

    def test_session_end_does_not_overwrite_a_send_confirmed_meanwhile(self):
        hid = self.handoff(session=SID, status="released")
        with self.settled_first("session_ended", "sent", hid):  # the transcript check confirmed delivery
            self.end_session()
        self.assertEqual(store.handoff(self.db, hid)["status"], "sent")
        self.assertEqual(self.codex.texts, [])  # Codex isn't told the draft was not sent
        self.assertNotIn("Claude session ended", self.notifications())

    def test_session_end_still_ends_an_open_report(self):
        hid = self.handoff(session=SID, status="released")
        self.end_session()
        self.assertEqual(store.handoff(self.db, hid)["status"], "session_ended")
        self.assertIn(intake.SESSION_ENDED_NOTICE, self.codex.texts[0])
        self.assertIn("Claude session ended", self.notifications())


if __name__ == "__main__":
    unittest.main()
