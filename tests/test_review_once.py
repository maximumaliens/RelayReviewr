"""Unit checks for #5: /relay-review forwards the last reply that isn't a reply to one of the relay's own commands,
and leaves a linked session's link as it was."""
import contextlib
import io
import json
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from support import RelayCase  # noqa: E402
from ccrelay import cli  # noqa: E402

SESSION = "abcdef0123456789"


class ReviewOnceCase(RelayCase):
    """One Claude session in a project: what the user typed, the relay commands they ran, and what Codex got."""

    def setUp(self):
        super().setUp()
        self.repo = self.project("Project")
        self.entries = []
        with mock.patch.dict(os.environ, {"RELAY_CLAUDE_HOME": str(self.root / "claude")}), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.install_claude()  # the command files as the user has them

    def said(self, prompt, reply):
        """A turn the user typed while the session wasn't linked, and Claude's reply."""
        self.entries += [{"type": "user", "message": {"role": "user", "content": prompt}}, assistant(reply)]

    def command(self, name, *args, **stop_payload):
        """The user types one of the relay's commands: it runs against the transcript so far, Claude echoes its
        output and that turn ends. Returns the output. A command the relay refuses (a non-zero exit) is
        different: Claude Code (2.1.322) shows the user its error and runs no turn, so no Stop hook follows."""
        out, refused = self.run_command(name, *args)
        if not refused:
            self.end_turn(SESSION, out, **stop_payload)
        return out

    def run_command(self, name, *args):
        """Run a relay command against the transcript so far and record it there, in the shape Claude Code wrote
        in acceptance tests 19 and 20, without ending its turn. Returns (output, whether it was refused)."""
        typed = {"type": "user", "message": {"role": "user", "content": (
            f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>\n"
            f"<command-args>{' '.join(args)}</command-args>")}}
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):  # as run_link runs the command
            os.environ.pop("CLAUDE_CONFIG_DIR", None)  # restored when the patch ends
            transcript = cli.transcript_path(str(self.repo), SESSION)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("".join(json.dumps(e) + "\n" for e in self.entries))
            try:
                if name == "relay-unlink":
                    out = self.run_unlink(SESSION).strip()
                else:
                    out = self.run_link(SESSION, self.repo, *args, once=name == "relay-review").strip()
            except SystemExit as e:
                self.entries += [typed, {"type": "user", "message": {"role": "user", "content": (
                    f"<local-command-stderr>Shell command failed for pattern \"!`relay`\": [stderr]\n{e.code}"
                    "</local-command-stderr>")}}]
                return str(e.code), True
        file_body = (self.root / "claude" / "commands" / f"{name}.md").read_text().split("---\n", 2)[2]
        self.entries += [
            typed,
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": [
                {"type": "text", "text": re.sub(r"!`[^`]*`", lambda _: out, file_body)}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": ""}]}},
            assistant(out)]
        return out, False

    @property
    def forwarded(self):
        """Each report Codex received, as queued."""
        return self.codex.texts


def first_sentence(reply):
    """What a shortened echo keeps: past live runs show Claude sometimes keeps only the first sentence."""
    return reply.split(". ")[0]


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class WhichReplyIsForwarded(ReviewOnceCase):
    def test_the_reply_to_relay_link_is_skipped(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "--as", "implementer", "T-1")
        self.command("relay-review", "--as", "implementer", "T-1")
        self.assertEqual(len(self.forwarded), 1)
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])
        self.assertNotIn("Linked this session", self.forwarded[0])

    def test_consecutive_relay_command_replies_are_skipped(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "T-1")
        self.command("relay-unlink")
        self.command("relay-review", "--as")  # refused: --as needs a role
        self.command("relay-review", "T-1")
        self.assertEqual(len(self.forwarded), 1)
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])

    def test_a_tool_result_inside_a_commands_turn_doesnt_make_its_reply_count(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "T-1")
        self.command("relay-unlink")
        self.entries += [{"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}, assistant("I also ran a command.")]
        self.command("relay-review", "T-1")
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])
        self.assertNotIn("I also ran a command.", self.forwarded[0])

    def test_a_prompt_without_text_ends_a_commands_turn(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "T-1")
        self.command("relay-unlink")
        self.entries += [{"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}]}},
            assistant("REPLY-TO-IMAGE")]
        self.command("relay-review", "T-1")
        self.assertIn("REPLY-TO-IMAGE", self.forwarded[0])

    def test_a_missing_transcript_is_reported_as_that(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "elsewhere")}), self.assertRaises(SystemExit) as e:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)  # restored when the patch ends
            cli.review_once(self.db, SESSION, str(self.repo), "T-1", None)
        self.assertIn("transcript wasn't found", str(e.exception.code))
        self.assertIn("Nothing was forwarded", str(e.exception.code))
        self.assertNotIn("no Claude reply yet", str(e.exception.code))

    def test_a_reply_made_after_a_relay_command_is_forwarded(self):
        self.said("build it", "FIRST-CLOSEOUT")
        self.command("relay-link", "T-1")
        self.command("relay-unlink")
        self.said("fix it", "SECOND-CLOSEOUT")
        self.command("relay-review", "T-1")
        self.assertIn("SECOND-CLOSEOUT", self.forwarded[0])

    def test_with_no_reply_of_claudes_own_it_says_so_and_forwards_nothing(self):
        self.command("relay-link", "T-1")
        self.command("relay-unlink")
        out = self.command("relay-review", "T-1")
        self.assertIn("no reply to forward", out)
        self.assertIn("Nothing was forwarded", out)
        self.assertEqual(self.forwarded, [])

    def test_a_refusal_in_a_linked_session_leaves_the_next_reply_to_be_forwarded(self):
        """A refused command runs no turn, so it must not ask the Stop hook to skip one."""
        self.command("relay-link", "T-1")
        out = self.command("relay-review", "T-1")
        self.assertIn("no reply to forward", out)
        self.assertEqual(self.forwarded, [])
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertEqual(len(self.forwarded), 1)
        self.assertIn("NEXT-REPLY", self.forwarded[0])


class TheSessionsOwnLink(ReviewOnceCase):
    def test_a_linked_session_is_still_linked_with_its_label_and_role_after_a_one_off(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "--as", "implementer", "T-1")
        self.command("relay-review", "--as", "implementer", "T-1")
        self.assertEqual(len(self.forwarded), 1, "forwarded once")
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertEqual(len(self.forwarded), 2, "the next reply is forwarded: the session is still linked")
        self.assertIn("NEXT-REPLY", self.forwarded[1])
        self.assertIn("ticket T-1 · from implementer", self.forwarded[1])

    def test_a_one_off_naming_neither_label_nor_role_uses_the_links(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "--as", "implementer", "T-1")
        out = self.command("relay-review")
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])
        self.assertIn("ticket T-1 · from implementer", self.forwarded[0])
        self.assertIn("stays linked to 'T-1'", first_sentence(out))

    def test_a_one_off_naming_another_label_and_role_says_what_happens_to_the_link(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "--as", "implementer", "T-1")
        out = self.command("relay-review", "--as", "verifier", "T-2")
        self.assertIn("for 'T-2' as verifier", first_sentence(out))
        self.assertIn("stays linked to 'T-1' as implementer, and its later replies go there", first_sentence(out))
        self.assertIn('relay review "T-2"', first_sentence(out), "the hint is still in the first sentence")
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])
        self.assertIn("ticket T-2 · from verifier", self.forwarded[0])
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertIn("NEXT-REPLY", self.forwarded[1])
        self.assertIn("ticket T-1 · from implementer", self.forwarded[1])

    def test_a_one_off_naming_only_another_role_says_what_happens_to_the_link(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "T-1")
        out = self.command("relay-review", "--as", "verifier")
        self.assertIn("for 'T-1' as verifier", first_sentence(out))
        self.assertIn("stays linked to 'T-1' as Project-abcdef", first_sentence(out))
        self.assertIn("ticket T-1 · from verifier", self.forwarded[0])
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertIn("ticket T-1 · from Project-abcdef", self.forwarded[1])

    def test_the_reply_says_the_link_stays_even_when_codex_cant_be_reached(self):
        from ccrelay import codexrpc
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "T-1")
        self.codex.fail = codexrpc.CodexError("boom")
        out, _ = self.run_command("relay-review", "T-2")
        self.assertIn("stays linked to 'T-1'", first_sentence(out))
        self.assertIn("couldn't be created yet", out)
        self.assertNotIn("\n", out, "one line: the command file has Claude echo the line verbatim")

    def test_a_one_off_skipped_for_background_tasks_is_dropped_and_the_link_carries_on(self):
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-link", "--as", "implementer", "T-1")
        self.command("relay-review", "--as", "verifier", "T-2", background_tasks=[{"id": "b1"}])
        self.assertEqual(self.forwarded, [], "a turn that ends with background tasks running isn't forwarded")
        told = (self.root / "state" / "notifications.log").read_text()
        self.assertIn("T-2: verifier one-off review skipped", told)
        self.assertIn("background tasks", told)
        self.assertIn("/relay-review again", told)
        self.assertIn("stays linked to 'T-1'", told)
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertEqual(len(self.forwarded), 1, "the skipped one-off isn't sent later in the next reply's place")
        self.assertIn("NEXT-REPLY", self.forwarded[0])
        self.assertIn("ticket T-1 · from implementer", self.forwarded[0])

    def test_an_unlinked_session_is_still_unlinked_after_a_one_off(self):
        self.said("build it", "BUILD-CLOSEOUT")
        out = self.command("relay-review", "T-1")
        self.assertNotIn("linked", out)
        self.assertEqual(len(self.forwarded), 1, "forwarded once")
        self.assertIn("BUILD-CLOSEOUT", self.forwarded[0])
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertEqual(len(self.forwarded), 1, "the next reply isn't forwarded: the session is unlinked")

    def test_a_second_one_off_before_the_first_was_forwarded_leaves_the_session_unlinked(self):
        """A turn that ends with background tasks running isn't forwarded, so the one-off waits for the next."""
        self.said("build it", "BUILD-CLOSEOUT")
        self.command("relay-review", "T-1", background_tasks=[{"id": "b1"}])
        self.command("relay-review", "T-1")
        self.assertEqual(len(self.forwarded), 1)
        self.end_turn(SESSION, "NEXT-REPLY")
        self.assertEqual(len(self.forwarded), 1, "the pending one-off wasn't taken for an ongoing link")


if __name__ == "__main__":
    unittest.main(verbosity=2)
