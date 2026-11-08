"""#3: the no-reply path (`no_reply_menu`) and the Discuss result that says Claude stays held."""
from unittest import mock

from support import RelayCase, StubServer
from ccrelay import claude_hooks, cli, intake, mcp_server, store

OTHER = "9999999999"


def tool_description(name):
    return next(t for t in mcp_server.TOOLS if t["name"] == name)["description"]


class NoReply(RelayCase):
    """A report being assessed (status 'assessing', no draft) in thread-1."""

    def setUp(self):
        super().setUp()
        self.enterContext(self.codex.installed())
        self.hid = self.handoff(status="assessing")
        self.ref = {"handoff": f"H{self.hid}"}

    def status(self, hid=None):
        return store.handoff(self.db, hid or self.hid)["status"]

    def notifications(self):
        path = store.relay_home() / "notifications.log"
        return path.read_text() if path.exists() else ""

    def drafts(self, hid=None):
        return self.db.execute("SELECT * FROM drafts WHERE handoff_id=?", (hid or self.hid,)).fetchall()

    def marker(self, hid=None):
        return store.no_reply(self.db, hid or self.hid)

    def no_reply(self, *choices, **kw):
        srv = StubServer(*choices, **kw)
        return srv, self.call(srv, "no_reply_menu", self.ref)

    def second_session(self):
        """Another session on the ticket, held with a draft of its own."""
        other = self.handoff(session=OTHER, status="review_ready")
        self.draft(other)
        return other


class TheMenu(NoReply):
    def test_offers_exactly_discuss_keep_waiting_and_finish_naming_the_source(self):
        srv, _ = self.no_reply("Keep waiting")
        name = store.session_name(store.handoff(self.db, self.hid))
        self.assertEqual(srv.forms[0]["choices"],
                         ["Discuss", "Keep waiting", f"Finish without sending — release {name}"])
        self.assertIn("needs no reply", srv.forms[0]["message"])

    def test_never_offers_send_or_copy_even_with_other_sessions_held(self):
        self.second_session()
        srv, _ = self.no_reply("Keep waiting")
        self.assertEqual([c for c in srv.forms[0]["choices"] if c.startswith(("Send", "Copy"))], [])

    def test_assessing_becomes_review_ready_with_a_marker_and_no_draft(self):
        self.no_reply("Keep waiting")
        self.assertEqual(self.status(), "review_ready")
        self.assertTrue(self.marker())
        self.assertEqual(self.drafts(), [], "no placeholder draft")

    def test_no__draft_hand_off_also_moves_to_review_ready(self):
        store.set_handoff(self.db, self.hid, "no_draft")
        self.no_reply("Keep waiting")
        self.assertEqual(self.status(), "review_ready")

    def test_nothing_is_finished_or_sent_without_the_owners_choice(self):
        _, result = self.no_reply("Keep waiting")
        self.assertIn("keeping Claude waiting", result)
        self.assertEqual(self.status(), "review_ready")
        self.assertEqual(self.drafts(), [])

    def test_finish_releases_the_source_only_with_nothing_sent(self):
        other = self.second_session()
        _, result = self.no_reply("Finish")
        self.assertEqual(self.status(), "finished")
        self.assertEqual(self.status(other), "review_ready", "the other session is untouched")
        self.assertEqual(self.drafts(), [])
        self.assertIn("released without a reply", result)

    def test_menu_shows_reports_waiting_for_assessment(self):
        later = self.handoff(session=OTHER, status="assessing")
        self.db.execute("UPDATE handoffs SET queue_item_id='item-1' WHERE id=?", (later,))
        self.codex.queues["thread-1"] = ["item-1"]
        srv, _ = self.no_reply("Keep waiting")
        self.assertIn(f"Reports waiting for assessment: H{later}", srv.forms[0]["message"])

    def test_the_open_menu_is_recorded_while_it_is_up(self):
        seen = []
        self.no_reply("Keep waiting", while_open=lambda: seen.append(store.open_menu(self.db, "thread-1")))
        self.assertEqual(seen[0]["handoff_id"], self.hid)
        self.assertIsNone(store.open_menu(self.db, "thread-1"))


class Persistence(NoReply):
    def test_show_menu_after_keep_waiting_shows_the_same_no_reply_menu(self):
        srv, _ = self.no_reply("Keep waiting")
        again = StubServer("Keep waiting")
        self.call(again, "show_menu", self.ref)
        self.assertEqual(again.forms[0]["choices"], srv.forms[0]["choices"])

    def test_show_menu_after_discuss_shows_the_same_no_reply_menu(self):
        srv, _ = self.no_reply("Discuss")
        again = StubServer("Keep waiting")
        self.call(again, "show_menu", self.ref)
        self.assertEqual(again.forms[0]["choices"], srv.forms[0]["choices"])

    def test_a_later_draft_brings_back_send(self):
        self.no_reply("Discuss")
        srv = StubServer("Keep waiting")
        self.call(srv, "propose_reply", {**self.ref, "draft": "do the thing"})
        self.assertTrue(any(c.startswith("Send to") for c in srv.forms[0]["choices"]))
        again = StubServer("Keep waiting")
        self.call(again, "show_menu", self.ref)
        self.assertTrue(any(c.startswith("Send to") for c in again.forms[0]["choices"]))
        self.assertNotIn("needs no reply", again.forms[0]["message"])

    def test_the_first_menu_for_a_draft_is_unchanged_without_a_marker(self):
        d = self.draft(self.hid)
        srv = StubServer("Keep waiting")
        self.call(srv, "show_menu", self.ref)
        self.assertTrue(any(c.startswith("Send to") for c in srv.forms[0]["choices"]))
        self.assertEqual(store.latest_draft(self.db, self.hid)["id"], d)


class Rejections(NoReply):
    def rejected(self, args, thread="thread-1"):
        srv = StubServer("Keep waiting")
        result = self.call(srv, "no_reply_menu", args, thread=thread)
        self.assertEqual(srv.forms, [], "no menu")
        self.assertFalse(self.marker(), "nothing written")
        return result

    def test_handoff_is_required(self):
        self.assertIn("name the handoff", self.rejected({}))
        self.assertEqual(tool_description("no_reply_menu") and
                         next(t for t in mcp_server.TOOLS if t["name"] == "no_reply_menu")["inputSchema"]["required"],
                         ["handoff"])

    def test_unknown_handoff(self):
        self.assertIn("does not exist", self.rejected({"handoff": "H999"}))

    def test_another_conversation(self):
        self.assertIn("belongs to another conversation", self.rejected(self.ref, thread="thread-2"))

    def test_closed_handoffs(self):
        for closed in ("superseded", "finished"):
            store.set_handoff(self.db, self.hid, closed)
            self.assertIn("is closed", self.rejected(self.ref))

    def test_a_sent_handoff(self):
        store.set_handoff(self.db, self.hid, "sent")
        self.assertIn("already sent", self.rejected(self.ref))
        self.assertEqual(self.status(), "sent")

    def test_a_handoff_that_has_a_draft_is_left_unchanged(self):
        d = self.draft(self.hid)
        before = self.db.execute("SELECT * FROM drafts WHERE id=?", (d,)).fetchone()
        result = self.rejected(self.ref)
        self.assertIn("already has a draft", result)
        self.assertEqual(self.status(), "assessing")
        self.assertEqual(dict(self.db.execute("SELECT * FROM drafts WHERE id=?", (d,)).fetchone()), dict(before))

    def test_a_rejected_call_leaves_the_detector_able_to_fire(self):
        self.draft(self.hid)  # rejected; then the draft is gone from the picture
        self.rejected(self.ref)
        self.db.execute("DELETE FROM drafts")
        claude_hooks.check_no_draft(self.db, store.load_config(), store.handoff(self.db, self.hid))
        self.assertEqual(self.status(), "no_draft")

    def test_after_the_source_stopped_it_says_so_and_changes_nothing(self):
        for stopped in ("released", "timed_out", "session_ended"):
            store.set_handoff(self.db, self.hid, stopped, "stopped")
            self.assertIn("no longer waiting", self.rejected(self.ref))
            self.assertEqual(self.status(), stopped)


class Detector(NoReply):
    def check(self, h=None):
        claude_hooks.check_no_draft(self.db, store.load_config(), h or store.handoff(self.db, self.hid))

    def test_still_fires_with_neither_a_draft_nor_a_no_reply_menu(self):
        self.check()
        self.assertEqual(self.status(), "no_draft")
        self.assertIn("assessment finished without a draft", self.notifications())

    def test_does_not_fire_for_a_no_reply_assessment(self):
        self.no_reply("Keep waiting")
        self.check()
        self.assertEqual(self.status(), "review_ready")
        self.assertNotIn("without a draft", self.notifications())

    def test_does_not_fire_in_the_race_where_the_hook_read_assessing_first(self):
        stale = store.handoff(self.db, self.hid)  # what the hook read just before the assessment was saved
        self.no_reply("Keep waiting")
        self.check(stale)
        self.assertEqual(self.status(), "review_ready")
        self.assertNotIn("without a draft", self.notifications())

    def test_a_marker_alone_stops_the_detector_whatever_the_status(self):
        self.db.execute("INSERT INTO no_reply_marks (handoff_id, created) VALUES (?, 0)", (self.hid,))
        self.check()
        self.assertEqual(self.status(), "assessing")
        self.assertNotIn("without a draft", self.notifications())


class Notification(NoReply):
    def test_the_first_assessment_notifies_once_naming_the_session(self):
        self.no_reply("Keep waiting")
        self.no_reply("Keep waiting")
        self.call(StubServer("Keep waiting"), "show_menu", self.ref)
        name = store.session_name(store.handoff(self.db, self.hid))
        line = f"T-1: {name} report H{self.hid}: review ready — no reply needed"
        self.assertEqual(self.notifications().count(line), 1)

    def test_a_failing_notification_changes_nothing(self):
        with mock.patch.object(mcp_server, "notify", side_effect=OSError("disk full")):
            srv, _ = self.no_reply("Keep waiting")
        self.assertEqual(len(srv.forms), 1, "the menu still opens")
        self.assertEqual(self.status(), "review_ready")
        self.assertTrue(self.marker())


class SourceStopsWaiting(NoReply):
    def test_the_open_menu_is_withdrawn_when_the_source_stops(self):
        srv, result = self.no_reply("Keep waiting", while_open=lambda: store.set_handoff(
            self.db, self.hid, "released", "Claude released the wait (Esc or exit)", only_from=store.WAITING))
        self.assertIn("withdrawn", result)
        self.assertEqual(self.status(), "released")

    def test_reopened_after_the_source_stopped_it_says_so_and_offers_no_keep_waiting(self):
        for stopped in ("released", "timed_out", "session_ended"):
            hid = self.handoff(session=f"s-{stopped}", status="assessing")
            self.call(StubServer("Keep waiting"), "no_reply_menu", {"handoff": f"H{hid}"})
            store.set_handoff(self.db, hid, stopped, "stopped")
            srv = StubServer("Finish")
            self.call(srv, "show_menu", {"handoff": f"H{hid}"})
            self.assertEqual(srv.forms[0]["choices"][0], "Discuss", stopped)
            self.assertFalse([c for c in srv.forms[0]["choices"] if c.startswith(("Keep", "Send", "Copy"))], stopped)
            self.assertIn("no longer waiting", srv.forms[0]["message"], stopped)
            self.assertEqual(self.status(hid), "finished", "Finish closes the record, nothing sent")
            self.assertEqual(self.drafts(hid), [])

    def test_a_marker_never_revives_a_stopped_hand_off(self):
        for stopped in ("released", "timed_out", "session_ended", "finished", "superseded", "sent"):
            hid = self.handoff(session=f"s-{stopped}", status=stopped)
            store.mark_no_reply(self.db, hid)
            self.assertEqual(self.status(hid), stopped)
            self.assertNotIn(self.status(hid), store.WAITING)

    def test_the_real_hook_stops_holding_when_the_source_is_finished(self):
        self.no_reply("Finish")
        p = self.start_wait_hook(self.hid)
        self.assertIsNone(self.hook_output(p), "released with no output: nothing sent")


class WithOtherProcesses(NoReply):
    """Hooks and the menu server started before the upgrade keep their old code."""

    def test_an_older_hooks_waiting_list_includes_review_ready_so_it_keeps_holding(self):
        self.no_reply("Keep waiting")
        old_waiting = ("queued", "assessing", "no_draft", "review_ready")
        self.assertIn(self.status(), old_waiting)

    def test_an_older_detector_acts_only_from_assessing_and_so_leaves_a_no_reply_alone(self):
        self.no_reply("Keep waiting")
        self.assertFalse(store.set_handoff(self.db, self.hid, "no_draft", "Codex finished without a draft",
                                           only_from=("assessing",)))
        self.assertEqual(self.status(), "review_ready")

    def test_no_new_status_is_introduced(self):
        self.assertEqual(store.WAITING, ("queued", "assessing", "no_draft", "review_ready"))

    def test_show_menu_with_neither_draft_nor_marker_still_answers_no_draft_yet(self):
        """What an older menu server answers for a no-reply hand-off too: it sends and finishes nothing."""
        srv = StubServer("Keep waiting")
        self.assertIn("No draft yet", self.call(srv, "show_menu", self.ref))
        self.assertEqual(srv.forms, [])
        self.assertEqual(self.status(), "assessing")


class DiscussKeepsClaudeHeld(NoReply):
    def test_the_discuss_result_says_claude_is_held_and_to_finish_first(self):
        self.draft(self.hid)
        result = self.call(StubServer("Discuss"), "show_menu", self.ref)
        self.assertIn("still held", result)
        self.assertIn("Finish first", result)
        self.assertIn("show_menu", result)
        self.assertIn("End your turn now", result)

    def test_the_no_reply_menu_discuss_result_says_the_same(self):
        _, result = self.no_reply("Discuss")
        self.assertIn("still held", result)
        self.assertIn("Finish first", result)
        self.assertIn("End your turn now", result)

    def test_not_held_any_more_it_does_not_claim_it_is(self):
        self.draft(self.hid, status="ready")
        store.set_handoff(self.db, self.hid, "released", "stopped")
        result = self.call(StubServer("Discuss"), "show_menu", self.ref)
        self.assertNotIn("still held", result)

    def test_a_queued_report_still_runs_first(self):
        later = self.handoff(session=OTHER, status="assessing")
        self.db.execute("UPDATE handoffs SET queue_item_id='item-1' WHERE id=?", (later,))
        self.codex.queues["thread-1"] = ["item-1"]
        _, result = self.no_reply("Discuss")
        self.assertIn(f"H{later}", result)
        self.assertIn("runs first", result)


class Guidance(NoReply):
    def test_operating_instructions(self):
        text = intake.OPS_INSTRUCTIONS
        for phrase in ("`no_reply_menu`", "don't draft an acknowledgement", "recommend Finish",
                       "comes back as another handoff", "still held", "Finish first"):
            self.assertIn(phrase, text)

    def test_tool_descriptions(self):
        self.assertIn("no_reply_menu", tool_description("propose_reply"))
        self.assertNotIn("Call after every assessment", tool_description("propose_reply"))
        for name in ("no_reply_menu", "show_menu"):
            self.assertIn("no-reply menu" if name == "show_menu" else "acknowledgement", tool_description(name))
        self.assertIn("another handoff", tool_description("no_reply_menu"))
        self.assertIn("recommend Finish", tool_description("no_reply_menu"))
        for name in ("propose_reply", "show_menu"):
            self.assertIn("Finish first", tool_description(name))

    def test_handoff_header_line(self):
        line = next(l for l in intake.handoff_text(store.handoff(self.db, self.hid)).splitlines()
                    if l.startswith("Reviewer instructions apply"))
        self.assertIn("no_reply_menu", line)
        self.assertIn("needs no instruction", line)

    def test_reviewer_config_approves_the_new_tool(self):
        import inspect
        self.assertIn("mcp_servers.relay.tools.no_reply_menu", inspect.getsource(cli))
