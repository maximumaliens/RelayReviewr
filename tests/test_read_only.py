"""Unit checks for #13: every reviewer conversation runs read-only, however it was created, loaded or opened, and
the relay doesn't queue into, or open, one it can't confirm is read-only. The fake Codex records each request the
relay sends and keeps a sandbox per conversation the way the development Codex daemon did in #13's probes."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from support import FakeCodex, RelayCase  # noqa: E402
from ccrelay import claude_hooks, cli, store  # noqa: E402

SESSION = "abcdef0123456789"
READ_ONLY = {"sandbox": "read-only", "approvalPolicy": "on-request"}


class ReadOnlyCase(RelayCase):
    def setUp(self):
        super().setUp()
        self.repo = self.project("Project")

    def link(self, label="T-1"):
        return self.run_link(SESSION, self.repo, label)

    def named(self, method):
        """What each `method` request named for the sandbox and the approval policy."""
        return [{k: p.get(k) for k in READ_ONLY} for p in self.codex.sent(method)]


class ANewConversation(ReadOnlyCase):
    def test_is_started_read_only(self):
        self.link()
        self.assertEqual(self.named("thread/start"), [READ_ONLY])

    def test_has_read_only_on_codex_record_so_a_later_plain_resume_keeps_it(self):
        """Codex doesn't record the sandbox a start names. A window opened by hand, or a reload after a daemon
        restart, resumes without naming one."""
        self.link()
        self.assertEqual(self.codex.recorded.get("thread-1"), FakeCodex.READ_ONLY)
        self.codex.restart()
        resumed = self.codex.call({}, "thread/resume", {"threadId": "thread-1"})
        self.assertEqual(resumed["sandbox"], FakeCodex.READ_ONLY)


class BeforeAReportIsQueued(ReadOnlyCase):
    def report(self):
        return self.run_stop_hook(SESSION, linked_in=self.repo)

    def methods(self):
        return [m for m, _ in self.codex.calls]

    def test_a_loaded_conversation_is_set_read_only_first(self):
        self.link()
        self.codex.calls.clear()
        self.report()
        self.assertIn("thread/resume", self.methods())
        self.assertLess(self.methods().index("thread/resume"), self.methods().index("thread/queue/add"))

    def test_a_conversation_running_with_write_access_becomes_read_only(self):
        self.link()
        self.codex.sandbox["thread-1"] = FakeCodex.WORKSPACE_WRITE  # as a window opened by hand left it
        self.report()
        self.assertEqual(self.codex.sandbox["thread-1"], FakeCodex.READ_ONLY)
        self.assertEqual(self.codex.recorded["thread-1"], FakeCodex.READ_ONLY)

    def test_every_resume_names_read_only_and_the_approval_policy(self):
        self.link()
        self.report()
        self.codex.restart()
        self.report()
        self.assertGreaterEqual(len(self.named("thread/resume")), 3)
        self.assertEqual([n for n in self.named("thread/resume") if n != READ_ONLY], [])

    def test_the_approval_policy_named_is_the_configured_one(self):
        granular = {"sandbox_approval": False, "rules": False, "mcp_elicitations": True}
        (self.root / "state" / "config.json").write_text(json.dumps({"approval_policy": granular}))
        self.link()
        self.assertEqual({p["approvalPolicy"]["granular"]["mcp_elicitations"] for p in self.codex.sent("thread/resume")},
                         {True})


class AConversationCodexDoesNotMakeReadOnly(ReadOnlyCase):
    """Codex answers the relay's read-only resume with workspace-write."""

    def setUp(self):
        super().setUp()
        self.link()
        self.codex.resume_sandbox = FakeCodex.WORKSPACE_WRITE

    def notifications(self):
        path = store.relay_home() / "notifications.log"
        return path.read_text() if path.exists() else ""

    def test_a_report_is_not_queued_and_its_hand_off_fails_with_a_notification(self):
        h = self.run_stop_hook(SESSION, linked_in=self.repo)
        self.assertEqual(self.codex.texts, [], "nothing was queued")
        self.assertEqual(h["status"], "failed")
        self.assertIn("this conversation is not read-only", h["reason"])
        self.assertIn(f"report H{h['id']}: assessment failed: this conversation is not read-only", self.notifications())

    def test_a_session_ended_notice_is_not_queued_either(self):
        self.codex.resume_sandbox = None
        h = self.run_stop_hook(SESSION, linked_in=self.repo)
        self.codex.texts.clear()
        self.codex.resume_sandbox = FakeCodex.WORKSPACE_WRITE
        with self.codex.installed(), mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": SESSION}))):
            claude_hooks.session_end()
        self.assertEqual(self.codex.texts, [])
        self.assertEqual(store.handoff(self.db, h["id"])["status"], "session_ended")
        self.assertIn("Codex could not be told", self.notifications())

    def test_linking_says_so_plainly(self):
        out = self.run_link("other-session-0001", self.repo, "T-1")
        self.assertIn("is not read-only", out)
        self.assertNotIn("couldn't be created", out)
        self.assertNotIn("relay review", out, "no hint to open a conversation the relay would refuse to open")
        self.assertEqual(out.count("\n"), 1, "one line: the command file has Claude echo the line verbatim")


class RelayReview(ReadOnlyCase):
    """`relay review <label>`, run from the project."""

    def setUp(self):
        super().setUp()
        self.link()

    def review(self):
        """Run the command; returns the argument list it starts Codex with, or None if it didn't."""
        with self.codex.installed(), mock.patch.object(os, "getcwd", return_value=str(self.repo)), \
                mock.patch.object(os, "chdir"), mock.patch.object(os, "execvpe") as execvpe:
            cli.review(["T-1"])
        return execvpe.call_args.args[1] if execvpe.called else None

    def test_opens_the_window_read_only_with_the_configured_approval_policy(self):
        self.assertEqual(self.review()[1:], ["resume", "-s", "read-only", "-a", "on-request", "thread-1"])

    def test_never_passes_a_config_override_or_a_profile(self):
        """Either one disconnects the window from the shared daemon (DESIGN.md)."""
        self.assertEqual({"-c", "--config", "-p", "--profile"} & set(self.review()), set())

    def test_an_approval_policy_the_codex_command_line_cannot_express_is_left_out(self):
        granular = {"sandbox_approval": False, "rules": False, "mcp_elicitations": True}
        (self.root / "state" / "config.json").write_text(json.dumps({"approval_policy": granular}))
        self.assertEqual(self.review()[1:], ["resume", "-s", "read-only", "thread-1"])

    def test_sets_a_conversation_with_write_access_read_only_before_opening_it(self):
        self.codex.sandbox["thread-1"] = FakeCodex.WORKSPACE_WRITE
        self.codex.calls.clear()
        self.assertIsNotNone(self.review())
        self.assertEqual(self.named("thread/resume"), [READ_ONLY])
        self.assertEqual(self.codex.sandbox["thread-1"], FakeCodex.READ_ONLY)

    def test_refuses_a_conversation_codex_does_not_make_read_only_and_says_why(self):
        self.codex.resume_sandbox = FakeCodex.WORKSPACE_WRITE
        with self.assertRaises(SystemExit) as refused:
            self.review()
        self.assertIn("this conversation is not read-only", str(refused.exception.code))
        self.assertIn("won't open it", str(refused.exception.code))


class InstallCodex(RelayCase):
    """`relay install-codex` makes read-only the reviewer Codex home's default sandbox."""

    def setUp(self):
        super().setUp()
        self.config = self.root / "codex" / "config.toml"
        (self.root / "state" / "config.json").write_text(json.dumps(
            {"codex_home": str(self.root / "codex"), "personas": {str(self.root / "Project"): ""}}))

    def install(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.install_codex()
        return out.getvalue()

    def test_run_twice_it_leaves_one_read_only_default_before_any_table(self):
        self.install()
        self.install()
        text = self.config.read_text()
        self.assertEqual(text.count("sandbox_mode"), 1)
        self.assertLess(text.index('sandbox_mode = "read-only"'), text.index("\n["))
        self.assertLess(text.index('sandbox_mode = "read-only"'), text.index("# --- end cc-codex-relay ---"),
                        "inside the relay's top-level block")
        self.assertEqual(tomllib.loads(text)["sandbox_mode"], "read-only")

    def test_the_users_own_write_default_is_set_aside_and_the_output_says_so(self):
        self.config.parent.mkdir()
        self.config.write_text('model = "m"\nsandbox_mode = "workspace-write"\n\n[tui]\nx = 1\n')
        out = self.install()
        self.install()
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["sandbox_mode"], "read-only")
        self.assertEqual((parsed["model"], parsed["tui"]), ("m", {"x": 1}), "the user's other settings are kept")
        self.assertIn('sandbox_mode = "workspace-write"', out)
        self.assertIn("commented out", out)
        self.assertIn('# sandbox_mode = "workspace-write"', self.config.read_text(), "kept, as a comment")

    def test_a_read_only_line_added_by_hand_after_the_relays_block_still_parses(self):
        """The workaround given before the fix: the line directly after the relay's top-level block."""
        self.install()
        text = self.config.read_text().replace("# --- end cc-codex-relay ---\n",
                                               '# --- end cc-codex-relay ---\nsandbox_mode = "read-only"\n', 1)
        self.config.write_text(text)
        self.install()
        self.assertEqual(tomllib.loads(self.config.read_text())["sandbox_mode"], "read-only")

    def test_an_array_spread_over_lines_does_not_hide_the_users_sandbox_mode(self):
        """A line of a multi-line array starts with a bracket, as a table header does."""
        self.config.parent.mkdir()
        self.config.write_text('notify = [\n  ["a", "b"],\n  ["c"]\n]\nsandbox_mode = "workspace-write"\n')
        self.install()
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["sandbox_mode"], "read-only")
        self.assertEqual(parsed["notify"], [["a", "b"], ["c"]])

    def test_a_result_that_would_not_parse_is_not_written(self):
        self.config.parent.mkdir()
        broken = 'model = "m"\nthis line is not TOML\n'
        self.config.write_text(broken)
        with self.assertRaises(SystemExit) as refused:
            self.install()
        self.assertIn("would not be valid TOML", str(refused.exception.code))
        self.assertEqual(self.config.read_text(), broken, "the file is left as it was")

    def test_a_sandbox_mode_inside_one_of_the_users_tables_is_left_alone(self):
        self.config.parent.mkdir()
        self.config.write_text('[profiles.mine]\nsandbox_mode = "workspace-write"\n')
        out = self.install()
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["sandbox_mode"], "read-only")
        self.assertEqual(parsed["profiles"]["mine"]["sandbox_mode"], "workspace-write")
        self.assertNotIn("commented out", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
