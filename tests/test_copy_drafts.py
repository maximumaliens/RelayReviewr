"""Seam 2: propose_reply with the real temporary store and a stubbed menu form. A draft is either free text or
copied from the named report by the relay, optionally followed by the reviewer's additions."""
import pathlib

from support import RelayCase, StubServer
from ccrelay import store

REPORT = "alpha\nbeta\ngamma\nbeta\ndelta\n"
PROVENANCE = "Copied from report H{h} (builder, Claude session abcdef01): {lines}; reviewer's additions: {n} characters."


class CopyCase(RelayCase):
    def held(self, report=REPORT, **kw):
        hid = self.handoff(report=report, **kw)
        self.db.execute("UPDATE handoffs SET display_name='builder' WHERE id=?", (hid,))
        return hid

    def propose(self, hid, args, *choices):
        srv = StubServer(*choices)
        return srv, self.call(srv, "propose_reply", {"handoff": f"H{hid}", **args})

    def draft_row(self, hid):
        return store.latest_draft(self.db, hid)

    def parts(self, did):
        return [tuple(r) for r in self.db.execute(
            "SELECT kind, source_handoff_id, start_char, end_char, from_line, to_line FROM draft_parts"
            " WHERE draft_id=? ORDER BY seq", (did,))]

    def snapshot(self):
        """Everything a rejected request must leave alone."""
        drafts = [tuple(r) for r in self.db.execute("SELECT * FROM drafts ORDER BY id")]
        parts = [tuple(r) for r in self.db.execute("SELECT * FROM draft_parts ORDER BY id")]
        files = {p.name: p.read_bytes() for p in (store.relay_home() / "drafts").glob("*")} \
            if (store.relay_home() / "drafts").exists() else {}
        return drafts, parts, files


class ValidCopies(CopyCase):
    def test_whole_report_is_stored_and_written_exactly(self):
        report = "line one  \r\n\tindented é日本\r\nlast, no newline"
        hid = self.held(report=report)
        srv, _ = self.propose(hid, {"copy": "all"})
        d = self.draft_row(hid)
        self.assertEqual(d["text"], "line one  \r\n\tindented é日本\r\nlast, no newline")
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(),
                         b"line one  \r\n\tindented \xc3\xa9\xe6\x97\xa5\xe6\x9c\xac\r\nlast, no newline")
        self.assertEqual(self.parts(d["id"]), [("copied", hid, 0, len(report), 1, 3)])

    def test_selections_followed_by_additions(self):
        hid = self.held()
        self.propose(hid, {"copy": [{"from": "alpha", "to": "beta", "to_line": 2}, {"from": "delta", "to": "delta"}],
                           "additions": "Amendments\nrun the suite first"})
        d = self.draft_row(hid)
        self.assertEqual(d["text"], "alpha\nbeta\n\ndelta\n\nAmendments\nrun the suite first")
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(),
                         b"alpha\nbeta\n\ndelta\n\nAmendments\nrun the suite first")
        self.assertEqual(self.parts(d["id"]), [
            ("copied", hid, 0, 11, 1, 2), ("separator", None, 11, 12, None, None),
            ("copied", hid, 12, 18, 5, 5), ("separator", None, 18, 19, None, None),
            ("additions", None, 19, 49, None, None)])

    def test_copy_comes_only_from_the_named_report(self):
        other = self.held(report="other report\n")
        hid = self.held(report="named report\n")
        self.propose(hid, {"copy": "all"})
        self.assertEqual(self.draft_row(hid)["text"], "named report\n")
        self.assertIsNone(self.draft_row(other))

    def test_a_new_proposal_replaces_the_earlier_ready_draft_as_before(self):
        hid = self.held()
        self.propose(hid, {"draft": "free text"})
        first = self.draft_row(hid)["id"]
        self.propose(hid, {"copy": "all"})
        self.assertEqual(self.draft_row(hid)["text"], REPORT)
        self.assertEqual(self.db.execute("SELECT status FROM drafts WHERE id=?", (first,)).fetchone()[0], "replaced")

    def test_null_for_the_unused_alternative_is_the_same_as_leaving_it_out(self):
        hid = self.held()
        self.propose(hid, {"draft": "free text", "copy": None, "additions": None})
        self.assertEqual(self.draft_row(hid)["text"], "free text")
        self.propose(hid, {"draft": None, "copy": "all"})
        self.assertEqual(self.draft_row(hid)["text"], REPORT)

    def test_a_relay_identifier_inside_copied_text_is_fine(self):
        hid = self.held(report="apply the corrective in H52\n")
        self.propose(hid, {"copy": "all"})
        self.assertEqual(self.draft_row(hid)["text"], "apply the corrective in H52\n")


class PlainDrafts(CopyCase):
    def test_free_text_is_stored_as_before_with_no_parts_and_no_provenance(self):
        hid = self.held()
        srv, _ = self.propose(hid, {"draft": "do it\r\nnow ✓\n", "recipient": "verifier"})
        d = self.draft_row(hid)
        self.assertEqual(d["text"], "do it\r\nnow ✓\n")
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(), "do it\r\nnow ✓\n".encode("utf-8"))
        self.assertEqual(self.parts(d["id"]), [])
        self.assertNotIn("Copied from", srv.forms[0]["message"])
        self.assertIn("Draft (sent exactly as stored):\ndo it\r\nnow ✓\n\n\nExact file:", srv.forms[0]["message"])
        self.assertEqual(d["suggested_recipient"], "verifier")

    def test_missing_or_blank_draft_keeps_its_message(self):
        hid = self.held()
        for args in ({}, {"draft": "  \n"}, {"draft": 5}):
            _, result = self.propose(hid, args)
            self.assertEqual(result, "Relay error: propose_reply needs a non-empty draft.")
        self.assertIsNone(self.draft_row(hid))

    def test_sent_handoff_still_refuses_either_kind(self):
        hid = self.held(status="sent")
        for args in ({"draft": "x"}, {"copy": "all"}):
            _, result = self.propose(hid, args)
            self.assertIn("is already sent", result)
        self.assertIsNone(self.draft_row(hid))


class Rejections(CopyCase):
    def test_every_invalid_request_leaves_the_existing_draft_and_files_alone(self):
        hid = self.held()
        self.propose(hid, {"copy": "all", "additions": "keep me"})
        before = self.snapshot()
        invalid = {
            "draft with copy": {"draft": "x", "copy": "all"},
            "draft with additions": {"draft": "x", "additions": "more"},
            "additions without copy": {"additions": "more"},
            "draft with copy and additions": {"draft": "x", "copy": "all", "additions": "more"},
            "unknown copy value": {"copy": "everything"},
            "empty selection list": {"copy": []},
            "selection missing to": {"copy": [{"from": "alpha"}]},
            "boundary not found": {"copy": [{"from": "omega", "to": "delta"}]},
            "ambiguous boundary": {"copy": [{"from": "alpha", "to": "beta"}]},
            "line number disagrees": {"copy": [{"from": "gamma", "from_line": 2, "to": "delta"}]},
            "overlapping selections": {"copy": [{"from": "alpha", "to": "gamma"}, {"from": "gamma", "to": "delta"}]},
            "whitespace-only additions": {"copy": "all", "additions": " \n"},
        }
        for name, args in invalid.items():
            with self.subTest(name):
                srv, result = self.propose(hid, args)
                self.assertTrue(result.startswith(("Rejected:", "Relay error:")), result)
                self.assertEqual(srv.forms, [], "no menu is shown for a rejected request")
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.draft_row(hid)["text"], REPORT + "\nkeep me")

    def test_rejection_with_no_earlier_draft_creates_nothing(self):
        hid = self.held()
        before = self.snapshot()
        srv, _ = self.propose(hid, {"draft": "x", "copy": "all"})
        self.assertEqual(srv.forms, [])
        self.assertEqual(self.snapshot(), before)

    def test_empty_report_is_rejected(self):
        hid = self.held(report="")
        srv, result = self.propose(hid, {"copy": "all"})
        self.assertIn("report is empty", result)
        self.assertIsNone(self.draft_row(hid))
        self.assertEqual(srv.forms, [])

    def test_ambiguous_boundary_lists_line_numbers_and_the_retry_copies_that_passage(self):
        hid = self.held()
        _, result = self.propose(hid, {"copy": [{"from": "alpha", "to": "beta"}]})
        self.assertIn("line 2: beta", result)
        self.assertIn("line 4: beta", result)
        self.assertIsNone(self.draft_row(hid))
        self.propose(hid, {"copy": [{"from": "alpha", "to": "beta", "to_line": 4}]})
        self.assertEqual(self.draft_row(hid)["text"], "alpha\nbeta\ngamma\nbeta\n")

    def test_other_conversation_and_closed_handoffs_are_still_rejected(self):
        other = self.held(thread="thread-2")
        closed = self.held(status="finished")
        for hid in (other, closed):
            _, result = self.propose(hid, {"copy": "all"})
            self.assertTrue(result.startswith("Rejected:"), result)
            self.assertIsNone(self.draft_row(hid))


class MenuProvenance(CopyCase):
    def test_provenance_is_shown_as_metadata_and_never_in_the_draft(self):
        hid = self.held()
        srv, _ = self.propose(hid, {"copy": [{"from": "alpha", "to": "beta", "to_line": 2},
                                             {"from": "delta", "to": "delta"}], "additions": "Reviewer notes\nok"})
        message = srv.forms[0]["message"]
        d = self.draft_row(hid)
        self.assertIn(PROVENANCE.format(h=hid, lines="lines 1-2 and 5", n=17), message)
        self.assertLess(message.index("Copied from report"), message.index("Draft (sent exactly as stored):"))
        self.assertIn("Draft (sent exactly as stored):\n" + d["text"] + "\n\nExact file: " + d["path"], message)
        self.assertNotIn("Copied from", d["text"])
        self.assertEqual(d["text"], "alpha\nbeta\n\ndelta\n\nReviewer notes\nok")

    def test_whole_report_without_additions_says_so(self):
        hid = self.held()
        srv, _ = self.propose(hid, {"copy": "all"})
        self.assertIn(PROVENANCE.format(h=hid, lines="lines 1-5", n=0), srv.forms[0]["message"])

    def test_a_long_copied_draft_gets_a_marked_preview_with_capped_additions_and_names_the_file(self):
        report = "".join(f"report line {i} é\n" for i in range(1, 201))
        hid = self.held(report=report)
        additions = "Reviewer notes\n" + "n" * 4000
        srv, _ = self.propose(hid, {"copy": "all", "additions": additions})
        message = srv.forms[0]["message"]
        d = self.draft_row(hid)
        self.assertEqual(d["text"], report + "\n" + additions)
        self.assertIn("shortened preview", message)
        self.assertIn("report line 1 é\nreport line 2 é\n", message)
        self.assertNotIn("report line 200", message)
        self.assertIn("Reviewer notes\n" + "n" * 100, message)
        self.assertNotIn("n" * 4000, message)
        self.assertIn(f"additions shortened: showing the first 1500 of {len(additions)} characters", message)
        self.assertIn(f"copied part shortened: showing the first 15 of 200 lines", message)
        self.assertIn(f"Exact file: {d['path']}", message)
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(), d["text"].encode("utf-8"))

    def test_the_copied_part_of_a_preview_is_capped_in_characters_too(self):
        report = "".join(f"{i} " + "w" * 2000 + "\n" for i in range(1, 21))
        hid = self.held(report=report)
        srv, _ = self.propose(hid, {"copy": "all"})
        message = srv.forms[0]["message"]
        self.assertIn("shortened preview", message)
        self.assertLess(len(message), 5000)
        self.assertIn(f"copied part shortened: showing the first 3000 of {len(report)} characters", message)
        self.assertEqual(self.draft_row(hid)["text"], report)

    def test_a_long_plain_draft_keeps_its_old_preview(self):
        hid = self.held()
        text = "".join(f"line {i}\n" for i in range(1, 61))
        srv, _ = self.propose(hid, {"draft": text})
        message = srv.forms[0]["message"]
        self.assertIn("Draft (sent exactly as stored):\nline 1\n", message)
        self.assertIn("line 15\n… (60 lines, 471 characters)", message)
        self.assertNotIn("line 16\n", message)
