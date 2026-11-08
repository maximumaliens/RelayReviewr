"""Seam 3: a copied draft of 8 KB or more goes through the real waiting hook unchanged. The transcript here is a
synthetic fixture written by the test in the shape the relay looks for; it shows the relay recognises that shape,
not that real Claude received or recorded anything. That claim belongs to the live acceptance check."""
import json

from support import RelayCase, StubServer
from ccrelay import claude_hooks, store

LINE = "Check ünïcode — café 日本語 and run `python3 -m unittest`  \t\r\n"
REPORT = "".join(f"{i:03d} {LINE}" for i in range(1, 161)) + "final line, no newline"
ADDITIONS = "Reviewer notes\r\nre-run the suite ✓\n"


class CopiedDelivery(RelayCase):
    def setUp(self):
        super().setUp()
        self.hid = self.handoff(report=REPORT, transcript=self.root / "claude.jsonl")
        self.db.execute("UPDATE handoffs SET display_name='builder' WHERE id=?", (self.hid,))

    def deliver(self):
        hook = self.start_wait_hook(self.hid)
        result = self.call(StubServer("Send to builder"), "propose_reply",
                           {"handoff": f"H{self.hid}", "copy": "all", "additions": ADDITIONS})
        return result, self.hook_output(hook)

    def test_fixture_is_large_enough_and_has_crlf_and_non_ascii(self):
        self.assertGreaterEqual(len(REPORT.encode("utf-8")), 8192)
        self.assertIn("\r\n", REPORT)
        self.assertTrue(any(ord(c) > 127 for c in REPORT))

    def test_hook_output_equals_the_stored_draft_which_equals_the_report_plus_additions(self):
        result, out = self.deliver()
        d = store.latest_draft(self.db, self.hid)
        self.assertEqual(result, "Sent to Claude. Claude is working; its next output will arrive here automatically.")
        self.assertEqual(out, {"decision": "block", "reason": d["text"]})
        self.assertEqual(out["reason"], REPORT + "\n\n" + ADDITIONS)  # report has no final newline: two LFs
        self.assertEqual(d["status"], "sent")
        self.assertEqual(open(d["path"], "rb").read(), out["reason"].encode("utf-8"))

    def test_the_relay_recognises_the_delivery_in_a_fixture_transcript(self):
        _, out = self.deliver()
        transcript = self.root / "claude.jsonl"
        entry = {"type": "user", "message": {"role": "user", "content": claude_hooks.FEEDBACK_PREFIX + out["reason"]}}
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
                              + json.dumps(entry) + "\n", encoding="utf-8")
        self.assertTrue(claude_hooks.transcript_has_feedback(str(transcript), out["reason"]))
        self.assertFalse(claude_hooks.transcript_has_feedback(str(transcript), out["reason"][:-1] + "!"))
