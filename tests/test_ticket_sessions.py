"""Who is on the ticket (#4): the menu, the tool results and the hand-off header list every linked session of the
same project and ticket label with its state, and a session that can't receive gets Copy with the reason. Links,
reports and skipped turns are made by the real commands and the real Stop hook against a fake Codex."""
import io
import itertools
import json
import os
from unittest import mock

from support import RelayCase, StubServer, git
from ccrelay import claude_hooks, cli, mcp_server, store

VERIFIER, BUILDER, TESTER = "aaaaaa1111111111", "bbbbbb2222222222", "cccccc3333333333"


class Ticket(RelayCase):
    def setUp(self):
        super().setUp()
        self.repo = self.project()
        # A steady clock, one second per reading. A session's state orders its link, its reports and its skipped
        # turns by recorded time; real turns are seconds apart, but these run within milliseconds, and the
        # development machine's wall clock has been seen stepping back by more than that.
        clock = itertools.count(1_800_000_000)
        patched = mock.patch.object(store, "now", lambda: float(next(clock)))
        patched.start()
        self.addCleanup(patched.stop)

    def link(self, session, role, cwd=None):
        """/relay-link in `session`, then the command's own turn, which is never reported."""
        self.run_link(session, cwd or self.repo, "--as", role, "T-1")
        self.end_turn(session, "Linked this session to 'T-1'")

    def report(self, session, message="report", **payload):
        """A finished turn of a linked session: the hand-off its Stop hook filed, or None if the turn was skipped."""
        before = self.db.execute("SELECT MAX(id) FROM handoffs").fetchone()[0] or 0
        h = self.end_turn(session, message, **payload)
        return h if h and h["id"] > before else None

    def menu(self, h, *choices, tool="show_menu", **args):
        srv = StubServer(*choices)
        result = self.call(srv, tool, {"handoff": f"H{h['id']}", **args}, thread=h["thread_id"])
        return srv, result

    def review_once(self, session, role, label="T-1"):
        """/relay-review in a session that has one reply of Claude's own, then the command's turn: the hand-off."""
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)  # restored when the patch ends
            transcript = cli.transcript_path(str(self.repo), session)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the reply to review once"}]}}) + "\n")
        self.run_link(session, self.repo, "--as", role, label, once=True)
        return self.report(session, "Forwarding the previous reply")

    def sessions_shown(self, srv):
        """The "Sessions on this ticket" lines of the first form shown."""
        lines = srv.forms[0]["message"].split("Sessions on this ticket:\n", 1)[1].split("\n\n", 1)[0]
        return lines.splitlines()


class LinkedSessionWithoutAReport(Ticket):
    def test_is_listed_as_no_turn_finished_since_linking_and_gets_copy(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        h = self.report(VERIFIER, "findings for the builder")
        srv, _ = self.menu(h, tool="propose_reply", draft="fix it")
        shown = self.sessions_shown(srv)
        self.assertEqual(len(shown), 2, shown)
        self.assertIn("verifier (source)", shown[0])
        self.assertEqual(shown[1], "- builder: not waiting (no turn finished since linking); Copy only")
        self.assertEqual(srv.forms[0]["choices"],
                         ["Send to verifier", "Copy for builder", "Copy to clipboard", "Discuss", "Keep waiting",
                          "Finish without sending — release verifier"])

    def test_a_session_linked_under_the_same_label_in_another_project_is_not_listed(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder", cwd=self.project("Other"))
        h = self.report(VERIFIER)
        srv, _ = self.menu(h, tool="propose_reply", draft="fix it")
        self.assertEqual(len(self.sessions_shown(srv)), 1, srv.forms[0]["message"])
        self.assertNotIn("builder", " ".join(srv.forms[0]["choices"]) + srv.forms[0]["message"])

    def test_a_worktree_of_the_project_is_the_same_ticket(self):
        worktree = self.root / "Project-wt"
        git("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=self.repo)
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder", cwd=worktree)
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertIn("Copy for builder", srv.forms[0]["choices"])


class StatesOfASessionThatIsNotHeld(Ticket):
    """Each is listed with its reason and gets Copy for …, never Send."""

    def setUp(self):
        super().setUp()
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")

    def builder_line(self):
        """How the builder is listed in the menu for a new verifier report, and that it is offered Copy only."""
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertIn("Copy for builder", srv.forms[0]["choices"])
        self.assertNotIn("Send to builder", srv.forms[0]["choices"])
        return next(l for l in self.sessions_shown(srv) if l.startswith("- builder"))

    def stopped(self, status, reason=None):
        """The builder reported, and its report then left the held states as the hook or the menu records it."""
        h = self.report(BUILDER)
        self.assertTrue(store.set_handoff(self.db, h["id"], status, reason, only_from=store.WAITING))
        return h

    def test_busy_released_timed_out_finished_and_failed_show_the_recorded_reason(self):
        for status, reason, shown in (
                ("sent", None, "busy: it received an instruction and is working"),
                ("released", "Claude released the wait (Esc or exit)", "released: Claude released the wait (Esc or exit)"),
                ("timed_out", "Claude's wait limit passed; nothing was sent",
                 "timed out: Claude's wait limit passed; nothing was sent"),
                ("finished", "the user finished without sending", "finished: the user finished without sending"),
                ("failed", "could not start the Codex assessment: down",
                 "failed: could not start the Codex assessment: down")):
            self.stopped(status, reason)
            self.assertEqual(self.builder_line(), f"- builder: not waiting ({shown}); Copy only", status)

    def test_a_turn_skipped_for_background_tasks_is_the_reason_until_the_session_reports(self):
        self.assertIsNone(self.report(BUILDER, background_tasks=[{"id": "bash-1"}]), "the turn was skipped")
        skipped = "- builder: not waiting (Last turn skipped because background work was running); Copy only"
        self.assertEqual(self.builder_line(), skipped)
        self.stopped("released", "Esc")
        self.assertEqual(self.builder_line(), "- builder: not waiting (released: Esc); Copy only")
        self.report(BUILDER, background_tasks=[{"id": "bash-2"}])
        self.assertEqual(self.builder_line(), skipped, "a skip after the newest report is the latest observation")

    def test_linking_again_starts_from_no_turn_finished_whatever_came_before(self):
        fresh = "- builder: not waiting (no turn finished since linking); Copy only"
        for status in ("sent", "released", "timed_out", "finished", "failed"):
            self.stopped(status, "an older report")
            self.link(BUILDER, "builder")
            self.assertEqual(self.builder_line(), fresh, f"after an older {status} report")
        self.report(BUILDER, background_tasks=[{"id": "bash-1"}])
        self.link(BUILDER, "builder")
        self.assertEqual(self.builder_line(), fresh, "after an older skipped turn")
        self.stopped("released", "Esc")
        self.assertEqual(self.builder_line(), "- builder: not waiting (released: Esc); Copy only",
                         "until it finishes a turn")

    def test_a_current_link_is_listed_even_when_the_clock_dates_an_old_ended_report_after_it(self):
        old = self.report(BUILDER)
        with self.codex.installed(), mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": BUILDER}))):
            claude_hooks.session_end()  # the real SessionEnd hook: the report ends and the link goes
        self.assertEqual(store.handoff(self.db, old["id"])["status"], "session_ended")
        self.link(BUILDER, "builder")  # the session was resumed and linked again
        linked = self.db.execute("SELECT created FROM links WHERE session_id=?", (BUILDER,)).fetchone()["created"]
        # The wall clock moved backwards in between, so the ended report carries a later time than the new link.
        self.db.execute("UPDATE handoffs SET created=?, updated=? WHERE id=?", (linked + 60, linked + 60, old["id"]))
        self.assertEqual(self.builder_line(), "- builder: not waiting (no turn finished since linking); Copy only")

    def test_an_unlinked_session_is_no_longer_listed(self):
        self.stopped("released", "Esc")
        self.run_unlink(BUILDER)
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertNotIn("builder", srv.forms[0]["message"])


class HeldSessions(Ticket):
    def test_a_one_off_reviews_session_is_offered_while_its_report_is_held_and_not_listed_afterwards(self):
        self.link(VERIFIER, "verifier")
        once = self.review_once(TESTER, "tester")
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertEqual(srv.forms[0]["choices"][:2], ["Send to verifier", "Send to tester"])
        store.set_handoff(self.db, once["id"], "released", "Esc", only_from=store.WAITING)
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertNotIn("tester", srv.forms[0]["message"] + " ".join(srv.forms[0]["choices"]))

    def test_a_linked_sessions_one_off_to_another_ticket_does_not_list_it_there_once_released(self):
        self.link(BUILDER, "builder")
        once = self.review_once(BUILDER, "builder", label="T-2")
        self.assertNotEqual(once["label"], "T-1")
        srv, _ = self.menu(once, tool="propose_reply", draft="fix it")
        self.assertEqual(srv.forms[0]["choices"][0], "Send to builder", "held there while its report is held")
        store.set_handoff(self.db, once["id"], "released", "Esc", only_from=store.WAITING)
        srv, _ = self.menu(once)
        self.assertEqual(srv.forms[0]["choices"],
                         ["Copy to clipboard", "Discuss", "Finish without sending — release builder"])
        self.assertNotIn("Sessions on this ticket", srv.forms[0]["message"])

    def test_same_named_sessions_are_told_apart_including_one_with_no_report(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        self.link(TESTER, "builder")
        self.report(TESTER)
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertEqual(srv.forms[0]["choices"][:3],
                         ["Send to verifier", "Send to builder (cccccc)", "Copy for builder (bbbbbb)"])
        self.assertIn("- builder (bbbbbb): not waiting (no turn finished since linking); Copy only",
                      self.sessions_shown(srv))

    def test_a_session_held_by_an_older_hook_is_listed_as_held_with_the_reason_and_gets_copy(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        self.link(TESTER, "tester")
        old = self.report(BUILDER)
        self.db.execute("UPDATE handoffs SET recipient_aware=NULL WHERE id=?", (old["id"],))  # filed before #6
        srv, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        self.assertEqual(srv.forms[0]["choices"],
                         ["Send to verifier", "Copy for builder", "Copy for tester", "Copy to clipboard", "Discuss",
                          "Keep waiting", "Finish without sending — release verifier"])
        builder = next(l for l in self.sessions_shown(srv) if l.startswith("- builder"))
        self.assertIn(f"H{old['id']}", builder)
        self.assertIn("Send isn't offered: it or the draft's source is held by an older relay hook", builder)


class SourceNotHeld(Ticket):
    def test_no_send_even_with_another_session_held_which_is_listed_with_the_reason_and_gets_copy(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        held = self.report(BUILDER)
        h = self.report(VERIFIER)
        self.menu(h, tool="propose_reply", draft="fix it")
        for status in ("released", "timed_out"):
            self.assertTrue(store.set_handoff(self.db, h["id"], status, "stopped",
                                              only_from=store.WAITING + ("released",)))
            srv, _ = self.menu(h)
            self.assertEqual(srv.forms[0]["choices"],
                             ["Copy for verifier", "Copy for builder", "Copy to clipboard", "Discuss",
                              "Finish without sending — release verifier"], status)
            shown = self.sessions_shown(srv)
            self.assertTrue(shown[0].startswith("- verifier (source): not waiting ("), shown)
            self.assertIn(f"H{held['id']}", shown[1])
            self.assertIn("Send isn't offered: the draft's source stopped waiting", shown[1])
        self.assertEqual(store.handoff(self.db, held["id"])["status"], "assessing", "the builder is still held")


    def test_an_uncertain_draft_keeps_its_menu_plain_copy_discuss_and_finish(self):
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        self.report(BUILDER)
        h = self.report(VERIFIER)
        self.menu(h, tool="propose_reply", draft="fix it")
        d = store.latest_draft(self.db, h["id"])
        self.db.execute("UPDATE drafts SET status='unknown' WHERE id=?", (d["id"],))  # an interrupted send
        expected = ["Copy to clipboard", "Discuss", "Finish without sending — release verifier"]
        self.assertEqual(self.menu(h)[0].forms[0]["choices"], expected, "while the source is held")
        self.assertTrue(store.set_handoff(self.db, h["id"], "released", "Esc", only_from=store.WAITING))
        srv, _ = self.menu(h)
        self.assertEqual(srv.forms[0]["choices"], expected, "and once it stopped waiting")
        self.assertIn("might have reached Claude", srv.forms[0]["message"])

    def test_a_source_that_is_not_listed_never_renames_a_listed_session(self):
        self.link(BUILDER, "builder")
        once = self.review_once(TESTER, "builder")  # an unlinked session reporting once under the same name
        store.set_handoff(self.db, once["id"], "released", "Esc", only_from=store.WAITING)
        srv, result = self.menu(once, tool="propose_reply", draft="fix it")
        listed = "- builder: not waiting (no turn finished since linking); Copy only"
        self.assertEqual(self.sessions_shown(srv), [listed])
        self.assertIn(listed, result, "the menu and Codex's list name the session alike")
        self.assertEqual(srv.forms[0]["choices"],
                         ["Copy for builder", "Copy to clipboard", "Discuss",
                          "Finish without sending — release builder (cccccc)"])


class PlainCopy(Ticket):
    def test_esc_is_discuss_and_copies_nothing(self):
        clipboard = self.fake_clipboard()
        self.link(VERIFIER, "verifier")
        h = self.report(VERIFIER)
        _, result = self.menu(h, None, tool="propose_reply", draft="fix it")
        self.assertTrue(result.startswith("The user wants to discuss."), result)
        self.assertFalse(clipboard.exists(), "nothing was put on the clipboard")
        self.assertEqual(store.latest_draft(self.db, h["id"])["status"], "ready", "the draft can still be sent")

    def test_is_offered_when_the_suggested_recipient_is_not_listed_and_the_suggestion_changes_nothing(self):
        self.link(VERIFIER, "verifier")
        plain, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it")
        hinted, _ = self.menu(self.report(VERIFIER), tool="propose_reply", draft="fix it", recipient="designer")
        self.assertEqual(hinted.forms[0]["choices"], plain.forms[0]["choices"])
        self.assertEqual(hinted.forms[0]["choices"][:2], ["Send to verifier", "Copy to clipboard"])
        self.assertIn("Reviewer suggests sending to: designer (a suggestion only)", hinted.forms[0]["message"])

    def test_copies_the_exact_draft_and_sends_nothing(self):
        clipboard = self.fake_clipboard()
        self.link(VERIFIER, "verifier")
        h = self.report(VERIFIER)
        hook = self.start_wait_hook(h["id"])
        _, result = self.menu(h, "Copy to clipboard", tool="propose_reply", draft="fix it ✓\n  exactly\n")
        self.assertTrue(result.startswith("Copied the draft to the user's clipboard. Nothing was sent to Claude."), result)
        self.assertEqual(clipboard.read_bytes().decode("utf-16-le"), "fix it ✓\n  exactly\n")
        self.assertIsNone(hook.poll(), "the source is still held, and received nothing")
        srv, _ = self.menu(h)
        self.assertFalse([c for c in srv.forms[0]["choices"] if c.startswith("Send")], "a copied draft isn't sent too")
        self.assertIn("This draft was copied to the clipboard, so Send is no longer offered for it.",
                      srv.forms[0]["message"])

    def test_after_the_draft_went_to_another_session_the_delivery_is_stated_and_stays_recorded(self):
        clipboard = self.fake_clipboard()
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        builder = self.report(BUILDER)
        h = self.report(VERIFIER)
        hook = self.start_wait_hook(builder["id"])
        self.menu(h, "Send to builder", tool="propose_reply", draft="fix the bug")
        self.assertEqual(self.hook_output(hook), {"decision": "block", "reason": "fix the bug"})
        d = store.latest_draft(self.db, h["id"])
        srv, result = self.menu(h, "Copy to clipboard")
        self.assertEqual(srv.forms[0]["choices"], ["Copy to clipboard", "Discuss", "Keep waiting",
                                                   "Finish without sending — release verifier"])
        lines = srv.forms[0]["message"].splitlines()
        self.assertTrue(lines[1].startswith(f"D{d['id']} was sent to builder (H{builder['id']})."), lines[:3])
        self.assertEqual(clipboard.read_bytes().decode("utf-16-le"), "fix the bug")
        after = store.latest_draft(self.db, h["id"])
        self.assertEqual((after["status"], after["recipient_handoff_id"]), ("sent", builder["id"]), result)
        self.assertEqual(store.handoff(self.db, h["id"])["status"], "review_ready", "the source is still held")


class WhatCodexIsTold(Ticket):
    def setUp(self):
        super().setUp()
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")

    def listed(self, text):
        """The session list at the end of a tool result or in a hand-off header."""
        self.assertIn("Sessions on this ticket", text)
        return [l for l in text.split("Sessions on this ticket", 1)[1].splitlines()[1:] if l.startswith("- ")]

    def test_propose_reply_and_show_menu_results_list_the_sessions_as_they_stand_on_return(self):
        h = self.report(VERIFIER)
        builder = "- builder: not waiting (no turn finished since linking); Copy only"
        _, result = self.menu(h, "Keep waiting", tool="propose_reply", draft="fix it")
        self.assertTrue(result.startswith("The user is keeping Claude waiting."), result)
        self.assertEqual(self.listed(result), [f"- verifier: held (H{h['id']})", builder])
        _, result = self.menu(h, "Finish without sending")
        self.assertTrue(result.startswith("Claude was released without a reply."), result)
        self.assertEqual(self.listed(result),
                         ["- verifier: not waiting (finished: the user finished without sending); Copy only", builder])

    def test_rejections_list_them_too(self):
        h = self.report(VERIFIER)
        newer = self.report(VERIFIER)  # supersedes h
        for tool, args in (("propose_reply", {"draft": "no handoff named"}),
                           ("show_menu", {"handoff": "H999"}),
                           ("propose_reply", {"handoff": f"H{h['id']}", "draft": "for a superseded report"}),
                           ("propose_reply", {"handoff": f"H{newer['id']}", "draft": "both", "copy": "all"})):
            srv = StubServer()
            result = self.call(srv, tool, args, thread=newer["thread_id"])
            self.assertTrue(result.startswith("Rejected:"), result)
            self.assertEqual(srv.forms, [])
            self.assertEqual(len(self.listed(result)), 2, result)

    def test_a_result_with_nobody_on_the_ticket_says_so(self):
        h = self.report(VERIFIER)
        self.run_unlink(VERIFIER)
        self.run_unlink(BUILDER)
        _, result = self.menu(h, "Finish without sending", tool="propose_reply", draft="fix it")
        self.assertTrue(result.endswith("Sessions on this ticket: none is held or linked."), result)

    def test_the_no_reply_menu_result_is_unchanged(self):
        h = self.report(VERIFIER)
        _, result = self.menu(h, "Keep waiting", tool="no_reply_menu")
        self.assertEqual(result, "The user is keeping Claude waiting. End your turn now with no further output; "
                                 "call show_menu if they later ask for the menu.")

    def test_each_hand_off_header_lists_the_sessions(self):
        h = self.report(VERIFIER, "findings")
        header = self.codex.texts[-1].split("--- Claude's final output ---")[0]
        self.assertEqual(self.listed(header),
                         [f"- verifier: held (H{h['id']})",
                          "- builder: not waiting (no turn finished since linking); Copy only"])
        self.report(BUILDER, background_tasks=[{"id": "bash-1"}])
        again = self.report(VERIFIER, "more findings")
        header = self.codex.texts[-1].split("--- Claude's final output ---")[0]
        self.assertEqual(self.listed(header),
                         [f"- verifier: held (H{again['id']})",
                          "- builder: not waiting (Last turn skipped because background work was running); Copy only"])

    def test_a_failure_to_list_the_sessions_never_blocks_a_report_or_hides_a_result(self):
        with mock.patch.object(store, "ticket_sessions", side_effect=RuntimeError("boom")):
            h = self.report(VERIFIER, "findings")
            self.assertEqual(h["status"], "assessing", "the report was filed and queued")
            self.assertNotIn("Sessions on this ticket", self.codex.texts[-1])
            result = self.call(StubServer(), "propose_reply", {"draft": "no handoff named"}, thread=h["thread_id"])
        self.assertTrue(result.startswith("Rejected: name the handoff"), result)
        self.assertNotIn("Relay error", result)


class FinishedReport(Ticket):
    """A report the owner finished stays finished; Codex may still prepare a Copy-only draft for it (amends Q8)."""

    def setUp(self):
        super().setUp()
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")
        self.held = self.report(BUILDER)
        self.h = self.report(VERIFIER, "line one\nline two\n")
        self.assertTrue(store.set_handoff(self.db, self.h["id"], "finished", "the user finished without sending",
                                          only_from=store.WAITING))

    def unchanged(self):
        self.assertEqual(store.handoff(self.db, self.h["id"])["status"], "finished")
        self.assertEqual(store.handoff(self.db, self.held["id"])["status"], "assessing")
        self.assertFalse(self.db.execute("SELECT 1 FROM drafts WHERE status IN ('approved', 'sending', 'sent')").fetchall())

    def test_a_free_text_draft_is_saved_and_its_menu_offers_copy_and_discuss_only(self):
        srv, result = self.menu(self.h, "Discuss", tool="propose_reply", draft="one more thing")
        self.assertEqual(store.latest_draft(self.db, self.h["id"])["text"], "one more thing")
        self.assertEqual(srv.forms[0]["choices"],
                         ["Copy for verifier", "Copy for builder", "Copy to clipboard", "Discuss"])
        message = srv.forms[0]["message"]
        self.assertIn("This report was finished without sending, so nothing can be sent for it.", message)
        self.assertIn("Draft (copied exactly as stored):\none more thing", message)
        for line in message.splitlines():  # no text shown for it says Send is available
            if "Send" in line or "sent" in line:
                self.assertRegex(line, r"nothing can be sent|Send isn't offered|never sent", message)
        self.assertTrue(result.startswith("The user wants to discuss."), result)
        self.assertNotIn("still held", result.split("\n\n")[0])
        self.unchanged()

    def test_a_suggested_recipient_is_not_worded_as_sending(self):
        srv, _ = self.menu(self.h, "Discuss", tool="propose_reply", draft="one more thing", recipient="builder")
        self.assertIn("Reviewer suggests this draft for: builder (a suggestion only)", srv.forms[0]["message"])
        self.assertNotIn("suggests sending", srv.forms[0]["message"])

    def test_copy_reads_the_finished_reports_stored_text(self):
        srv, _ = self.menu(self.h, "Discuss", tool="propose_reply", copy="all", additions="Reviewer notes")
        self.assertEqual(store.latest_draft(self.db, self.h["id"])["text"], "line one\nline two\n\nReviewer notes")
        self.assertNotIn("Keep waiting", srv.forms[0]["choices"])
        self.unchanged()

    def test_show_menu_reopens_that_menu_and_copy_never_reaches_a_waiting_hook(self):
        clipboard = self.fake_clipboard()
        hook = self.start_wait_hook(self.held["id"])  # the builder's real waiting hook, beside the Copy-only draft
        first, _ = self.menu(self.h, "Discuss", tool="propose_reply", draft="one more thing")
        again, result = self.menu(self.h, "Copy for builder")
        self.assertEqual(again.forms[0]["choices"], first.forms[0]["choices"])
        self.assertTrue(result.startswith("Copied the draft to the user's clipboard. Nothing was sent to Claude."))
        self.assertEqual(clipboard.read_bytes().decode("utf-16-le"), "one more thing")
        self.assertIsNone(hook.poll(), "the held builder's hook is still waiting and collected nothing")
        self.unchanged()

    def test_a_rejected_call_writes_nothing(self):
        srv, result = self.menu(self.h, tool="propose_reply", draft="both", copy="all")
        self.assertTrue(result.startswith("Rejected:"), result)
        self.assertEqual(srv.forms, [])
        self.assertIsNone(store.latest_draft(self.db, self.h["id"]))

    def test_show_menu_without_a_draft_and_no_reply_menu_still_reject_it(self):
        for tool in ("show_menu", "no_reply_menu"):
            srv, result = self.menu(self.h, tool=tool)
            self.assertTrue(result.startswith(f"Rejected: handoff H{self.h['id']} is closed (finished"), result)
            self.assertEqual(srv.forms, [])
        self.menu(self.h, "Discuss", tool="propose_reply", draft="one more thing")
        srv, result = self.menu(self.h, tool="no_reply_menu")
        self.assertTrue(result.startswith(f"Rejected: handoff H{self.h['id']} is closed (finished"), result)
        self.unchanged()

    def test_a_superseded_report_stays_closed_to_new_drafts(self):
        old = self.report(BUILDER)
        self.report(BUILDER)  # supersedes it
        for tool, args in (("propose_reply", {"draft": "too late"}), ("show_menu", {}), ("no_reply_menu", {})):
            srv, result = self.menu(old, tool=tool, **args)
            self.assertTrue(result.startswith(f"Rejected: handoff H{old['id']} is closed (superseded"), result)
            self.assertEqual(srv.forms, [])
        self.assertIsNone(store.latest_draft(self.db, old["id"]))

    def test_the_first_draft_raises_a_notice_that_does_not_offer_send(self):
        self.menu(self.h, "Discuss", tool="propose_reply", draft="one more thing")
        notice = self.db.execute("SELECT detail FROM events WHERE kind='notify' AND handoff_id=? ORDER BY id DESC",
                                 (self.h["id"],)).fetchone()["detail"]
        self.assertIn("draft ready to copy", notice)
        self.assertNotIn("Send", notice)


class NoteToTheOwner(Ticket):
    NOTE = "Reviewer's note to you (never sent): Send this to builder once it's held."

    def setUp(self):
        super().setUp()
        self.link(VERIFIER, "verifier")
        self.link(BUILDER, "builder")

    def test_the_menu_shows_it_above_the_draft_and_show_menu_shows_it_again(self):
        h = self.report(VERIFIER)
        srv, _ = self.menu(h, "Keep waiting", tool="propose_reply", draft="fix it",
                           note="  Send this to builder once it's held. ")
        message = srv.forms[0]["message"]
        self.assertIn(self.NOTE, message.splitlines())
        self.assertLess(message.index(self.NOTE), message.index("Sessions on this ticket:"))
        self.assertLess(message.index(self.NOTE), message.index("Draft ("))
        again, _ = self.menu(h, "Keep waiting")
        self.assertIn(self.NOTE, again.forms[0]["message"].splitlines())

    def test_a_revised_draft_carries_its_own_note_or_none(self):
        h = self.report(VERIFIER)
        self.menu(h, "Discuss", tool="propose_reply", draft="fix it", note="Send this to builder once it's held.")
        revised, _ = self.menu(h, "Discuss", tool="propose_reply", draft="fix it properly")
        self.assertNotIn("note to you", revised.forms[0]["message"])
        third, _ = self.menu(h, "Discuss", tool="propose_reply", draft="fix it at once", note="Send it now.")
        self.assertIn("Reviewer's note to you (never sent): Send it now.", third.forms[0]["message"])
        self.assertNotIn("once it's held", third.forms[0]["message"])

    def test_delivery_and_the_draft_file_are_byte_identical_with_and_without_a_note(self):
        text, delivered, files = "fix it ✓\r\n  exactly\n", [], []
        for note in ({"note": "Send this to the verifier; the note must never arrive."}, {}):
            h = self.report(VERIFIER)
            hook = self.start_wait_hook(h["id"])
            self.menu(h, "Send to verifier", tool="propose_reply", draft=text, **note)
            delivered.append(self.hook_output(hook))
            files.append(open(store.latest_draft(self.db, h["id"])["path"], "rb").read())
        self.assertEqual(delivered, [{"decision": "block", "reason": text}] * 2)
        self.assertEqual(files, [text.encode("utf-8")] * 2)

    def test_null_or_blank_counts_as_absent(self):
        h = self.report(VERIFIER)
        for note in (None, "", " \n\t"):
            srv, _ = self.menu(h, "Discuss", tool="propose_reply", draft="fix it", note=note)
            self.assertEqual(len(srv.forms), 1, repr(note))
            self.assertNotIn("note to you", srv.forms[0]["message"])

    def test_a_non_string_is_rejected_before_anything_is_written(self):
        h = self.report(VERIFIER)
        self.menu(h, "Discuss", tool="propose_reply", draft="the draft that stays")
        kept = store.latest_draft(self.db, h["id"])
        for note in (5, ["Send it"], {"text": "Send it"}, True):
            srv, result = self.menu(h, tool="propose_reply", draft="a replacement", note=note)
            self.assertTrue(result.startswith("Rejected: note"), result)
            self.assertIn("Nothing was saved or shown.", result)
            self.assertEqual(srv.forms, [])
        now = store.latest_draft(self.db, h["id"])
        self.assertEqual((now["id"], now["text"], now["status"]), (kept["id"], "the draft that stays", "ready"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)

    def test_after_a_send_elsewhere_the_earlier_delivery_is_still_stated_first(self):
        builder = self.report(BUILDER)
        h = self.report(VERIFIER)
        hook = self.start_wait_hook(builder["id"])
        self.menu(h, "Send to builder", tool="propose_reply", draft="fix the bug", note="Send this to builder.")
        self.hook_output(hook)
        message = self.menu(h, "Keep waiting")[0].forms[0]["message"]
        self.assertLess(message.index("was sent to builder"), message.index("Reviewer's note to you"))
