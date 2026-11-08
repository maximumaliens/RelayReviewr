"""Seam 4: wording coverage. The reviewer's guidance on isolated sessions, standalone drafts and the copy path is
present in the operating instructions, the propose_reply tool description and the per-hand-off line. This checks
the text the relay supplies; it does not show that an existing Codex conversation has adopted it."""
import re

from support import RelayCase
from ccrelay import intake, mcp_server, store


def propose_reply_tool():
    return next(t for t in mcp_server.TOOLS if t["name"] == "propose_reply")


class OperatingInstructions(RelayCase):
    def test_states_isolation_and_standalone_delivery(self):
        text = intake.OPS_INSTRUCTIONS.lower()
        self.assertIn("sessions are isolated", text)
        self.assertIn("must stand alone", text)
        self.assertRegex(text, r"exact files and commands")
        self.assertRegex(text, r"must not depend on (relay )?identifiers")

    def test_gives_builder_to_verifier_guidance(self):
        text = intake.OPS_INSTRUCTIONS.lower()
        self.assertIn("forward the entire closeout unchanged", text)
        self.assertIn("corrective", text)
        self.assertIn("never replace it with a summary", text)
        self.assertIn("reviewer notes", text)
        self.assertIn("with nothing material to add, forward the closeout alone", text)

    def test_gives_verifier_to_builder_guidance(self):
        text = intake.OPS_INSTRUCTIONS.lower()
        self.assertIn("accepted builder-directed instructions", text)
        self.assertIn("leave out", text)
        self.assertIn("amendments", text)
        self.assertIn("conflicting requirements", text)
        self.assertIn("explain your assessment decisions to the user separately", text)

    def test_describes_both_paths_and_forbids_mixing_them(self):
        text = intake.OPS_INSTRUCTIONS.lower()
        self.assertIn("`copy`", text)
        self.assertIn("`additions`", text)
        self.assertIn("never `draft` together with `copy`", text)

    def test_stays_generic(self):
        for text in (intake.OPS_INSTRUCTIONS, propose_reply_tool()["description"]):
            self.assertIsNone(re.search(r"\bH\d+\b", text.replace("H<n>", "")), "no concrete relay numbers")


class ToolDescription(RelayCase):
    def test_description_carries_the_guidance(self):
        text = propose_reply_tool()["description"]
        for phrase in ("isolated", "must stand alone", "Reviewer notes", "builder-directed",
                       "never `draft` together with `copy`", "copy \"all\""):
            self.assertIn(phrase, text)

    def test_only_handoff_is_required_and_both_paths_are_offered(self):
        schema = propose_reply_tool()["inputSchema"]
        self.assertEqual(schema["required"], ["handoff"])
        self.assertTrue({"draft", "copy", "additions", "handoff", "recipient"} <= set(schema["properties"]))


class PerHandoffLine(RelayCase):
    def test_line_names_both_paths_and_standalone(self):
        h = store.handoff(self.db, self.handoff())
        text = intake.handoff_text(h)
        line = next(l for l in text.splitlines() if l.startswith("Reviewer instructions apply"))
        self.assertIn("draft", line)
        self.assertIn("copy", line)
        self.assertIn("additions", line)
        self.assertIn("stand alone", line)
        self.assertIn(f'handoff "H{h["id"]}"', line)
