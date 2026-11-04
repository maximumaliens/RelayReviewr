"""Every draft and menu names the report (hand-off) it answers; wording is generic."""
import os
import pathlib
import re
import unittest

import support
from support import RelayCase, StubServer
from ccrelay import intake, store


class RequiredHandoff(RelayCase):
    def assertRejected(self, text, *needles):
        self.assertTrue(text.startswith("Not saved") or text.startswith("Rejected"), text)
        for n in needles:
            self.assertIn(n, text)

    def test_propose_reply_without_a_handoff_is_rejected_and_stores_nothing(self):
        self.handoff()
        srv = StubServer()
        text = self.call(srv, "propose_reply", {"draft": "fix it"})
        self.assertRejected(text, "handoff")
        self.assertEqual(srv.forms, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_show_menu_without_a_handoff_is_rejected(self):
        h = self.handoff()
        self.draft(h)
        srv = StubServer()
        self.assertRejected(self.call(srv, "show_menu", {}), "handoff")
        self.assertEqual(srv.forms, [])

    def test_handoff_from_another_conversation_is_rejected(self):
        self.handoff(thread="thread-1")
        other = self.handoff(label="T-2", thread="thread-2")
        srv = StubServer()
        text = self.call(srv, "propose_reply", {"draft": "fix it", "handoff": f"H{other}"})
        self.assertRejected(text, f"H{other}", "another conversation")
        self.assertEqual(srv.forms, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_unknown_handoff_is_rejected(self):
        self.handoff()
        self.assertRejected(self.call(StubServer(), "show_menu", {"handoff": "H999"}), "H999")

    def test_malformed_handoff_is_rejected(self):
        self.handoff()
        for bad in ("five", "", None, [], True):
            self.assertRejected(self.call(StubServer(), "show_menu", {"handoff": bad}), "handoff")

    def test_closed_handoff_is_rejected(self):
        old = self.handoff(status="superseded")
        self.handoff()
        srv = StubServer()
        text = self.call(srv, "propose_reply", {"draft": "fix it", "handoff": f"H{old}"})
        self.assertRejected(text, f"H{old}", "superseded")
        self.assertEqual(srv.forms, [])

    def test_finished_handoff_is_closed_but_released_timed_out_and_ended_stay_open(self):
        fin = self.handoff(status="finished")
        text = self.call(StubServer(), "show_menu", {"handoff": f"H{fin}"})
        self.assertRejected(text, f"H{fin}", "finished")
        for status in ("released", "timed_out", "session_ended"):
            h = self.handoff(status=status)
            self.draft(h)
            srv = StubServer("Keep waiting")
            self.call(srv, "show_menu", {"handoff": f"H{h}"})
            self.assertEqual(srv.forms[0]["choices"][0], "Copy to clipboard", status)

    def test_sent_handoff_still_refuses_drafting_as_today(self):
        h = self.handoff(status="sent")
        text = self.call(StubServer(), "propose_reply", {"draft": "fix it", "handoff": f"H{h}"})
        self.assertIn("already sent", text)

    def test_no_fallback_to_the_newest_handoff(self):
        older = self.handoff()
        newest = self.handoff()
        srv = StubServer("Keep waiting")
        self.call(srv, "propose_reply", {"draft": "for the older report", "handoff": f"H{older}"})
        rows = self.db.execute("SELECT handoff_id FROM drafts").fetchall()
        self.assertEqual([r[0] for r in rows], [older])
        self.assertIsNone(store.latest_draft(self.db, newest))
        # and a call naming nothing does not attach to the newest either
        self.call(StubServer(), "propose_reply", {"draft": "orphan"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)

    def test_handoff_accepts_h_prefix_or_bare_number(self):
        h = self.handoff()
        for ref in (f"H{h}", f"h{h}", str(h), h):
            self.draft(h)
            srv = StubServer("Keep waiting")
            self.call(srv, "show_menu", {"handoff": ref})
            self.assertEqual(len(srv.forms), 1, ref)


class MenuNamesTheReport(RelayCase):
    def test_first_line_shows_the_source_report(self):
        h = self.handoff(label="ABC-42 triage")
        srv = StubServer("Keep waiting")
        self.call(srv, "propose_reply", {"draft": "fix it", "handoff": f"H{h}"})
        first = srv.forms[0]["message"].splitlines()[0]
        self.assertIn(f"H{h}", first)
        self.assertIn("ABC-42 triage", first)

    def test_menu_is_for_the_named_report_not_the_newest(self):
        older = self.handoff()
        self.draft(older, "older draft")
        newer = self.handoff()
        self.draft(newer, "newer draft")
        srv = StubServer("Keep waiting")
        self.call(srv, "show_menu", {"handoff": f"H{older}"})
        self.assertIn("older draft", srv.forms[0]["message"])
        self.assertNotIn("newer draft", srv.forms[0]["message"])

    def test_suggested_recipient_is_text_only(self):
        h = self.handoff()
        plain = StubServer("Keep waiting")
        self.call(plain, "propose_reply", {"draft": "fix it", "handoff": f"H{h}"})
        hinted = StubServer("Keep waiting")
        self.call(hinted, "propose_reply", {"draft": "fix it v2", "handoff": f"H{h}", "recipient": "implementer"})
        self.assertEqual(plain.forms[0]["choices"], hinted.forms[0]["choices"])
        self.assertIn("implementer", hinted.forms[0]["message"])
        self.assertNotIn("implementer", plain.forms[0]["message"])

    def test_suggestion_survives_a_later_show_menu(self):
        h = self.handoff()
        self.call(StubServer("Keep waiting"), "propose_reply",
                  {"draft": "fix it", "handoff": f"H{h}", "recipient": "implementer"})
        srv = StubServer("Keep waiting")
        self.call(srv, "show_menu", {"handoff": f"H{h}"})
        self.assertIn("implementer", srv.forms[0]["message"])


HOME_PATH = re.compile(r"/home/[a-z_][a-z0-9_-]*/|/Users/[^/\s]+/|[A-Za-z]:\\Users\\")
# Names that must not appear in the relay's wording, passed in as a regex (e.g. RELAY_PRIVATE_NAMES='alice|acme')
# so the names themselves stay out of the repository. None when unset.
PRIVATE_NAMES = re.compile(os.environ["RELAY_PRIVATE_NAMES"], re.I) if os.environ.get("RELAY_PRIVATE_NAMES") else None


class GenericToolResults(RelayCase):
    def test_tool_results_and_menus_are_generic(self):
        texts = []
        for choice in ("Keep waiting", "Finish without sending", "Discuss"):
            h = self.handoff()
            srv = StubServer(choice)
            texts.append(self.call(srv, "propose_reply", {"draft": "d", "handoff": f"H{h}"}))
            texts.append(srv.forms[0]["message"])
        for t in texts:
            self.assertIsNone(HOME_PATH.search(t), t)
            if PRIVATE_NAMES:
                self.assertIsNone(PRIVATE_NAMES.search(t), t)


class DraftsStillWork(RelayCase):
    def test_draft_is_stored_against_the_named_report_and_review_ready(self):
        h = self.handoff(status="assessing")
        self.call(StubServer("Keep waiting"), "propose_reply", {"draft": "fix it", "handoff": f"H{h}"})
        self.assertEqual(store.latest_draft(self.db, h)["text"], "fix it")
        self.assertEqual(store.handoff(self.db, h)["status"], "review_ready")

    def test_send_reaches_the_waiting_hook_byte_for_byte(self):
        h = self.handoff(status="assessing")
        hook = self.start_wait_hook(h)
        text = "line one\n  indented ✓\n"
        result = self.call(StubServer("Send"), "propose_reply", {"draft": text, "handoff": f"H{h}"})
        self.assertIn("Sent to Claude", result)
        self.assertEqual(self.hook_output(hook), {"decision": "block", "reason": text})


class GenericWording(unittest.TestCase):
    ROOT = pathlib.Path(support.ROOT)
    def package_texts(self):
        for path in sorted((self.ROOT / "ccrelay").rglob("*")):
            if path.suffix in (".py", ".md"):
                yield path, path.read_text()

    def test_no_home_folder_paths_in_the_relay_package(self):
        for path, text in self.package_texts():
            self.assertIsNone(HOME_PATH.search(text), f"{path} hardcodes a home folder")

    @unittest.skipUnless(PRIVATE_NAMES, "set RELAY_PRIVATE_NAMES to a regex of names to keep out")
    def test_no_personal_or_project_names_in_the_relay_package(self):
        for path, text in self.package_texts():
            self.assertIsNone(PRIVATE_NAMES.search(text), f"{path} names the user or a project")

    def test_operating_instructions_cover_the_contract(self):
        text = intake.OPS_INSTRUCTIONS
        self.assertIn("the user", text)
        self.assertIn("H<n>", text)
        self.assertRegex(text, r"(?i)never claim the user answered")
        self.assertRegex(text, r"(?i)recipient.*optional|optional.*recipient")

    def test_handoff_header_tells_codex_to_name_the_report(self):
        h = {"id": 7, "created": 0.0, "label": "T", "session_id": "abcdef0123456789", "claude_message": "x"}
        self.assertIn("H7", intake.handoff_text(h).split("\n\n")[0])
        self.assertIn("handoff", intake.handoff_text(h).split("\n")[1])

    def test_standing_context_heading_and_ended_notice_are_generic(self):
        block = intake.handover_block({"path": "p", "text": "ctx", "sha256": "s", "mtime": 0.0})
        self.assertIn("the user", block.lower())
        self.assertIn("the user", intake.SESSION_ENDED_NOTICE)



if __name__ == "__main__":
    unittest.main()
