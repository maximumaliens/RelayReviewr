"""Send to a chosen recipient (#6): several held sessions on one project and ticket share a conversation; the
user picks who receives each draft, and a recipient that can't take it is reported plainly."""
import io
import json
import threading
import time
from unittest import mock

from support import RelayCase, StubServer
from ccrelay import claude_hooks, codexrpc, store


class Recipients(RelayCase):
    """Two sessions on one ticket, as the Stop hook leaves them: the verifier reported (the source) and the
    implementer is held too."""

    def held(self, role, session, thread="thread-1", label="T-1", **kw):
        hid = self.handoff(label=label, thread=thread, session=session, status="review_ready", **kw)
        self.db.execute("UPDATE handoffs SET display_name=?, branch='main', head='abc1234' WHERE id=?", (role, hid))
        return hid

    def menu(self, hid, *choices):
        srv = StubServer(*choices)
        result = self.call(srv, "show_menu", {"handoff": f"H{hid}"})
        return srv, result


class Eligible(Recipients):
    def test_one_send_per_held_session_on_the_ticket_source_first(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        self.held("other-ticket", "3333333333", thread="thread-2", label="T-2")
        self.draft(ver)
        srv, _ = self.menu(ver)
        self.assertEqual(srv.forms[0]["choices"],
                         ["Send to verifier", "Send to implementer", "Discuss", "Keep waiting",
                          "Finish without sending — release verifier"])

    def test_same_label_in_another_project_and_sessions_no_longer_waiting_are_not_offered(self):
        ver = self.held("verifier", "2222222222")
        self.held("other-project", "3333333333", thread="thread-other-project")  # same label T-1, other project
        gone = self.held("released", "4444444444")
        self.db.execute("UPDATE handoffs SET status='released' WHERE id=?", (gone,))
        dead = self.held("dead-hook", "5555555555", hook_pid=2 ** 22 + 1)
        self.draft(ver)
        srv, _ = self.menu(ver)
        self.assertEqual([c for c in srv.forms[0]["choices"] if c.startswith("Send")], ["Send to verifier"])
        self.assertEqual(store.handoff(self.db, dead)["status"], "released", "a dead hook is found out, not offered")

    def test_two_sessions_with_the_same_name_are_told_apart_by_session_id(self):
        a = self.held("verifier", "aaaaaa0000")
        self.held("verifier", "bbbbbb0000")
        self.draft(a)
        srv, _ = self.menu(a)
        self.assertEqual(srv.forms[0]["choices"][:2], ["Send to verifier (aaaaaa)", "Send to verifier (bbbbbb)"])


class Delivery(Recipients):
    def test_send_to_another_session_reaches_only_it_and_the_source_stays_held(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver, "fix the bug\n  exactly ✓\n")
        impl_hook, ver_hook = self.start_wait_hook(impl), self.start_wait_hook(ver)
        _, result = self.menu(ver, "Send to implementer")
        self.assertEqual(self.hook_output(impl_hook), {"decision": "block", "reason": "fix the bug\n  exactly ✓\n"})
        self.assertIsNone(ver_hook.poll(), "the source's hook is still waiting")
        self.assertEqual(store.handoff(self.db, ver)["status"], "review_ready")
        self.assertEqual(store.handoff(self.db, impl)["status"], "sent")
        self.assertEqual(result, f"Sent D{d} to implementer (H{impl}). Verifier H{ver} is still waiting; "
                                 f"use show_menu H{ver} to send it something or finish it.")

    def test_finish_releases_only_the_source(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        self.draft(ver)
        impl_hook, ver_hook = self.start_wait_hook(impl), self.start_wait_hook(ver)
        self.menu(ver, "Finish without sending")
        self.assertIsNone(self.hook_output(ver_hook), "the source is released with nothing sent")
        self.assertIsNone(impl_hook.poll(), "the other held session is still waiting")
        self.assertEqual(store.handoff(self.db, impl)["status"], "review_ready")


class RecipientGone(Recipients):
    def test_recipient_that_stopped_waiting_while_the_menu_was_open_gets_the_not_sent_menu(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver)

        def implementer_pressed_esc():
            store.set_handoff(self.db, impl, "released", "Claude released the wait (Esc or exit)")

        srv = StubServer("Send to implementer", "Keep waiting", while_open=implementer_pressed_esc)
        self.call(srv, "show_menu", {"handoff": f"H{ver}"})
        self.assertEqual(len(srv.forms), 2, "the menu returns once, after Send; it is not redrawn while open")
        again = srv.forms[1]
        released_at = store.handoff(self.db, impl)["updated"]
        self.assertTrue(again["message"].startswith(
            f"Not sent: implementer stopped waiting at {time.strftime('%H:%M', time.localtime(released_at))} "
            "(Claude released the wait (Esc or exit))"), again["message"])
        self.assertEqual(again["choices"], ["Copy for implementer", "Send to verifier", "Discuss", "Keep waiting",
                                            "Finish without sending — release verifier"])
        self.assertEqual(store.latest_draft(self.db, ver)["status"], "ready", f"D{d} was not sent or approved")


class UncertainDelivery(Recipients):
    def interrupted_send_to_implementer(self, impl_transcript):
        """Send a verifier draft to the implementer and kill the implementer's hook between 'sending' and output."""
        (store.relay_home() / "config.json").write_text('{"send_pickup_secs": 2}')  # not the real 15 s deadline
        impl = self.held("implementer", "1111111111", transcript=impl_transcript)
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver, "fix the bug")
        hook = self.start_wait_hook(impl, pause_before_output=30)
        self.db.execute("UPDATE handoffs SET hook_pid=? WHERE id=?", (hook.pid, impl))
        result = {}
        sender = threading.Thread(target=lambda: result.update(
            text=self.call(StubServer("Send to implementer"), "show_menu", {"handoff": f"H{ver}"})))
        sender.start()
        deadline = time.time() + 10
        while store.latest_draft(self.db, ver)["status"] != "sending" and time.time() < deadline:
            time.sleep(0.1)
        hook.kill()
        hook.wait()
        sender.join(30)
        return impl, ver, d, result["text"]

    def test_interrupted_send_ends_unknown_and_no_send_is_offered_to_anyone(self):
        impl, ver, d, result = self.interrupted_send_to_implementer(self.root / "impl.jsonl")
        self.assertIn("might have reached", result)
        self.assertEqual(store.latest_draft(self.db, ver)["status"], "unknown")
        self.held("another", "6666666666")  # another held session appears; still no Send of this draft
        srv, _ = self.menu(ver)
        self.assertFalse([c for c in srv.forms[0]["choices"] if c.startswith("Send")], srv.forms[0]["choices"])
        self.assertIn("might have reached Claude", srv.forms[0]["message"])
        self.assertNotIn("Not sent", srv.forms[0]["message"], "uncertain is never shown as an ordinary not sent")

    def test_interrupted_send_found_in_the_recipients_transcript_counts_as_sent(self):
        transcript = self.root / "impl.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "Stop hook feedback:\nfix the bug"}})
                              + "\n")
        impl, ver, d, result = self.interrupted_send_to_implementer(transcript)
        self.assertEqual(store.latest_draft(self.db, ver)["status"], "sent")
        self.assertEqual(store.handoff(self.db, impl)["status"], "sent")
        self.assertIn(store.handoff(self.db, ver)["status"], store.WAITING, "the source is still held")
        self.assertIn(f"Sent D{d} to implementer", result)


class EarlierDraftClosed(Recipients):
    def test_recipients_own_unsent_draft_is_closed_once_it_receives_another(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        own = self.draft(impl, "the implementer's own draft")
        d = self.draft(ver, "the verifier's findings")
        hook = self.start_wait_hook(impl)
        self.menu(ver, "Send to implementer")
        self.assertEqual(self.hook_output(hook)["reason"], "the verifier's findings")
        srv, _ = self.menu(impl)
        self.assertFalse([c for c in srv.forms[0]["choices"] if c.startswith("Send")], f"D{own} can't be sent later")
        self.assertIn(f"Not sent: the session received D{d} instead", srv.forms[0]["message"])


class MenuText(Recipients):
    def test_each_destination_shows_folder_branch_head_and_capture_time(self):
        impl = self.held("implementer", "1111111111", cwd=self.root / "Proj-wt")
        ver = self.held("verifier", "2222222222", cwd=self.root / "Proj")
        self.db.execute("UPDATE handoffs SET branch='fix-x', head='def5678' WHERE id=?", (impl,))
        self.draft(ver)
        srv, _ = self.menu(ver)
        msg = srv.forms[0]["message"]
        for hid, folder, branch, head in ((ver, "Proj", "main", "abc1234"), (impl, "Proj-wt", "fix-x", "def5678")):
            captured = time.strftime("%H:%M", time.localtime(store.handoff(self.db, hid)["created"]))
            line = next((l for l in msg.splitlines() if str(self.root / folder) + " " in l + " "), "")
            for needle in (f"branch {branch}", f"HEAD {head}", f"captured {captured}"):
                self.assertIn(needle, line, msg)
        self.assertLess(msg.index("verifier"), msg.index("implementer"), "the source is listed first")


class CopyForSessionsNotHeld(Recipients):
    def test_busy_released_or_timed_out_sessions_on_the_ticket_are_offered_copy(self):
        ver = self.held("verifier", "2222222222")
        for role, sid, status in (("implementer", "1111111111", "sent"), ("tester", "3333333333", "released"),
                                  ("writer", "4444444444", "timed_out"), ("gone", "5555555555", "session_ended")):
            hid = self.held(role, sid)
            self.db.execute("UPDATE handoffs SET status=? WHERE id=?", (status, hid))
            self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, 'T-1', ?, 0)",
                            (sid, str(self.root)))
        self.draft(ver)
        srv, _ = self.menu(ver)
        self.assertEqual(srv.forms[0]["choices"],
                         ["Send to verifier", "Copy for implementer", "Copy for tester", "Copy for writer",
                          "Discuss", "Keep waiting", "Finish without sending — release verifier"])
        self.assertIn("implementer: not waiting (busy", srv.forms[0]["message"])


class NextHeaderNamesLastInstruction(Recipients):
    def test_recipients_next_handoff_header_names_the_instruction_and_its_source(self):
        impl = self.held("implementer", "1111111111", cwd=self.project("Proj"))
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver, "fix the bug")
        hook = self.start_wait_hook(impl)
        self.menu(ver, "Send to implementer")
        self.hook_output(hook)
        queued = []
        self.db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, 'T-1', 'thread-1', ?, 0)",
                        (str((self.root / "Proj").resolve()), str(self.root / "Proj")))
        self.db.execute("INSERT INTO links (session_id, label, cwd, role, created) VALUES "
                        "('1111111111', 'T-1', ?, 'implementer', 0)", (str(self.root / "Proj"),))
        payload = {"session_id": "1111111111", "transcript_path": "/t.jsonl", "last_assistant_message": "fixed it"}
        with mock.patch.object(codexrpc, "ensure_daemon"), mock.patch.object(codexrpc, "ensure_loaded"), \
                mock.patch.object(codexrpc, "queue_message", lambda cfg, t, text, cid: queued.append(text)), \
                mock.patch.object(claude_hooks, "wait", lambda db, cfg, hid: 0), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            claude_hooks.stop()
        header = queued[0].split("--- Claude's final output ---")[0]
        self.assertIn(f"Last instruction delivered: D{d}, source verifier H{ver}", header)


class SourceAfterSendingElsewhere(Recipients):
    def test_show_menu_on_the_still_waiting_source_lets_the_user_finish_it(self):
        impl = self.held("implementer", "1111111111")
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver, "fix the bug")
        impl_hook, ver_hook = self.start_wait_hook(impl), self.start_wait_hook(ver)
        self.menu(ver, "Send to implementer")
        self.hook_output(impl_hook)
        srv, result = self.menu(ver, "Finish without sending")
        self.assertEqual(srv.forms[0]["choices"], ["Discuss", "Keep waiting", "Finish without sending — release verifier"])
        self.assertIn(f"D{d} was sent to implementer (H{impl})", srv.forms[0]["message"])
        self.assertIsNone(self.hook_output(ver_hook), "the verifier is released with nothing more sent")
        self.assertIn("released without a reply", result)


class ReviewFixes(Recipients):
    """From the #6 review."""

    def test_hand_offs_filed_before_recipients_existed_can_only_be_sent_to_their_own_session(self):
        # an upgrade while sessions are held: their hooks run the old code, which claims by report, not recipient
        old_impl = self.held("implementer", "1111111111")
        old_ver = self.held("verifier", "2222222222")
        new = self.held("tester", "3333333333")
        self.db.execute("UPDATE handoffs SET recipient_aware=NULL WHERE id IN (?, ?)", (old_impl, old_ver))
        self.draft(old_ver)
        srv, _ = self.menu(old_ver)
        self.assertEqual([c for c in srv.forms[0]["choices"] if c.startswith("Send")], ["Send to verifier"])
        self.draft(new)
        srv, _ = self.menu(new)
        self.assertEqual([c for c in srv.forms[0]["choices"] if c.startswith("Send")], ["Send to tester"])

    def test_a_draft_approved_before_recipients_existed_goes_to_its_own_session(self):
        ver = self.held("verifier", "2222222222")
        d = self.draft(ver, "approved by the old menu server", status="approved")
        self.assertEqual(self.hook_output(self.start_wait_hook(ver))["reason"], "approved by the old menu server")
        self.assertEqual(store.latest_draft(self.db, ver)["status"], "sent", f"D{d}")

    def test_a_recipient_still_waiting_that_does_not_collect_in_time_is_not_reported_as_stopped(self):
        (store.relay_home() / "config.json").write_text('{"send_pickup_secs": 1}')
        self.held("implementer", "1111111111")  # held, but no hook process collects anything
        ver = self.held("verifier", "2222222222")
        self.draft(ver)
        srv, result = self.menu(ver, "Send to implementer")
        self.assertEqual(len(srv.forms), 1)
        self.assertIn("did not collect", result)
        self.assertNotIn("stopped waiting", result)

    def test_menu_after_a_send_elsewhere_is_withdrawn_when_the_source_stops_waiting(self):
        impl, ver = self.held("implementer", "1111111111"), self.held("verifier", "2222222222")
        d = self.draft(ver)
        self.db.execute("UPDATE drafts SET status='sent', recipient_handoff_id=? WHERE id=?", (impl, d))
        srv = StubServer("Keep waiting", while_open=lambda: store.set_handoff(self.db, ver, "released", "Esc"))
        result = self.call(srv, "show_menu", {"handoff": f"H{ver}"})
        self.assertIn("withdrawn", result)

    def test_finish_does_not_overwrite_a_source_that_has_since_been_sent_something(self):
        impl, ver = self.held("implementer", "1111111111"), self.held("verifier", "2222222222")
        d = self.draft(ver)
        self.db.execute("UPDATE drafts SET status='sent', recipient_handoff_id=? WHERE id=?", (impl, d))
        srv = StubServer("Finish without sending", while_open=lambda: store.set_handoff(self.db, ver, "sent"))
        srv_result = self.call(srv, "show_menu", {"handoff": f"H{ver}"})
        self.assertEqual(store.handoff(self.db, ver)["status"], "sent", srv_result)

    def test_sent_elsewhere_and_source_gone_names_the_session_that_received_it(self):
        impl, ver = self.held("implementer", "1111111111"), self.held("verifier", "2222222222")
        d = self.draft(ver)
        self.db.execute("UPDATE drafts SET status='sent', recipient_handoff_id=? WHERE id=?", (impl, d))
        store.set_handoff(self.db, ver, "released", "Esc")
        result = self.call(StubServer(), "show_menu", {"handoff": f"H{ver}"})
        self.assertIn(f"D{d} went to implementer (H{impl})", result)

    def test_same_names_are_told_apart_in_finish_and_copy_too(self):
        a = self.held("implementer", "aaaaaa0000")
        self.held("implementer", "bbbbbb0000")
        self.draft(a)
        srv, _ = self.menu(a)
        self.assertEqual(srv.forms[0]["choices"][-1], "Finish without sending — release implementer (aaaaaa)")

    def test_a_second_draft_is_not_approved_for_a_recipient_already_receiving_one(self):
        impl, ver = self.held("implementer", "1111111111"), self.held("verifier", "2222222222")
        first = self.draft(impl, "the implementer's own draft", status="approved")
        self.db.execute("UPDATE drafts SET recipient_handoff_id=? WHERE id=?", (impl, first))
        d = self.draft(ver)
        _, result = self.menu(ver, "Send to implementer")
        self.assertIn(f"D{first}", result)
        self.assertEqual(store.latest_draft(self.db, ver)["status"], "ready", f"D{d} was not approved")

    def test_copy_is_offered_only_to_sessions_still_linked_to_the_ticket_including_failed_ones(self):
        ver = self.held("verifier", "2222222222")
        moved = self.held("moved-on", "3333333333")
        failed = self.held("failed", "4444444444")
        self.db.execute("UPDATE handoffs SET status='sent' WHERE id=?", (moved,))
        self.db.execute("UPDATE handoffs SET status='failed', reason='could not start' WHERE id=?", (failed,))
        for sid, label in (("3333333333", "T-2"), ("4444444444", "T-1")):
            self.db.execute("INSERT INTO links (session_id, label, cwd, created) VALUES (?, ?, ?, 0)",
                            (sid, label, str(self.root)))
        self.draft(ver)
        srv, _ = self.menu(ver)
        copies = [c for c in srv.forms[0]["choices"] if c.startswith("Copy")]
        self.assertEqual(copies, ["Copy for failed"])
