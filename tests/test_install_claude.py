"""Installer safety tests for `relay install-claude` / `uninstall-claude`, against a throwaway Claude home."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

RELAY = str(pathlib.Path(__file__).resolve().parents[1] / "bin" / "relay")
CMDS = ("relay-link.md", "relay-unlink.md", "relay-review.md")


class InstallClaude(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.claude = root / "claude"
        self.claude.mkdir()
        self.env = dict(os.environ, RELAY_CLAUDE_HOME=str(self.claude), RELAY_HOME=str(root / "state"))
        self.settings = self.claude / "settings.json"
        self.user_settings = {
            "model": "opus",
            "env": {"KEEP_ME": "1"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own hook-stop", "timeout": 5}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]},
        }
        self.settings.write_text(json.dumps(self.user_settings))

    def tearDown(self):
        self.tmp.cleanup()

    def relay(self, *args):
        return subprocess.run([RELAY, *args], env=self.env, capture_output=True, text=True)

    def read(self):
        return json.loads(self.settings.read_text())

    def test_refuses_when_foreign_command_file_exists(self):
        (self.claude / "commands").mkdir()
        mine = self.claude / "commands" / "relay-link.md"
        mine.write_text("my own command\n")
        before = self.settings.read_text()
        r = self.relay("install-claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not written by the relay", r.stderr)
        self.assertEqual(mine.read_text(), "my own command\n", "foreign file untouched")
        self.assertEqual(self.settings.read_text(), before, "settings untouched")
        self.assertEqual(list(self.claude.glob("settings.json.relay-backup-*")), [], "no backup written either")
        self.assertFalse((self.claude / "commands" / "relay-unlink.md").exists(), "no commands written")

    def test_command_output_is_fenced_last_and_not_for_claude_to_run(self):
        """Live runs (#9): Claude copied an instruction or the marker placed after the output, and ran the relay review
        command the output names. The output now comes last, inside <relay-reply>, after an instruction not to run it."""
        self.assertEqual(self.relay("install-claude").returncode, 0)
        for name, sub in (("relay-link.md", "link"), ("relay-review.md", "review-once")):
            text = (self.claude / "commands" / name).read_text()
            self.assertTrue(text.endswith(f"<relay-reply>\n!`{RELAY} {sub} $ARGUMENTS`\n</relay-reply>\n"), text)
            self.assertIn("Don't run any command it names", text)

    def test_install_backs_up_and_keeps_user_settings(self):
        r = self.relay("install-claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        backups = list(self.claude.glob("settings.json.relay-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.user_settings, "backup is the pre-install file")
        s = self.read()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["env"]["KEEP_ME"], "1")
        self.assertEqual(s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"], "1000")
        stop_cmds = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("my-own hook-stop", stop_cmds, "a user hook whose name contains hook-stop is kept")
        self.assertIn(f"{RELAY} hook-stop", stop_cmds)
        self.assertEqual(s["hooks"]["PreToolUse"], self.user_settings["hooks"]["PreToolUse"])
        for c in CMDS:
            self.assertTrue((self.claude / "commands" / c).exists())

    def test_reinstall_updates_own_files_without_duplicating_hooks(self):
        self.relay("install-claude")
        r = self.relay("install-claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        stop_cmds = [h["command"] for g in self.read()["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop_cmds.count(f"{RELAY} hook-stop"), 1)

    def test_reinstall_refreshes_managed_commands_with_the_as_hint(self):
        self.relay("install-claude")
        link = self.claude / "commands" / "relay-link.md"
        link.write_text(link.read_text().replace("[--as <role>] [label]", "[label]"))  # an older install
        self.assertEqual(self.relay("install-claude").returncode, 0)
        for name in ("relay-link.md", "relay-review.md"):
            self.assertIn("argument-hint: [--as <role>] [label]", (self.claude / "commands" / name).read_text())

    def test_uninstall_removes_only_relay_additions(self):
        self.relay("install-claude")
        s = self.read()  # the user changes settings after installing
        s["env"]["ADDED_LATER"] = "x"
        s["hooks"].setdefault("SessionStart", []).append({"hooks": [{"type": "command", "command": "later"}]})
        self.settings.write_text(json.dumps(s))
        r = self.relay("uninstall-claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.read()
        self.assertNotIn("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", s["env"], "cap removed (there was none before)")
        self.assertEqual(s["env"]["ADDED_LATER"], "x")
        self.assertEqual(s["env"]["KEEP_ME"], "1")
        all_cmds = [h["command"] for ev in s["hooks"].values() for g in ev for h in g["hooks"]]
        self.assertFalse([c for c in all_cmds if c.startswith(RELAY)], "no relay hooks left")
        self.assertIn("my-own hook-stop", all_cmds)
        self.assertIn("later", all_cmds)
        for c in CMDS:
            self.assertFalse((self.claude / "commands" / c).exists())

    def test_uninstall_restores_previous_cap_and_respects_later_change(self):
        s = self.read()
        s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "20"
        self.settings.write_text(json.dumps(s))
        self.relay("install-claude")
        self.relay("uninstall-claude")
        self.assertEqual(self.read()["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"], "20", "previous value restored")
        self.relay("install-claude")
        s = self.read()
        s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "50"  # the user changed it after install
        self.settings.write_text(json.dumps(s))
        self.relay("uninstall-claude")
        self.assertEqual(self.read()["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"], "50", "later change left alone")

    def test_uninstall_keeps_foreign_command_with_same_name(self):
        self.relay("install-claude")
        f = self.claude / "commands" / "relay-review.md"
        f.write_text("The user replaced this\n")
        self.relay("uninstall-claude")
        self.assertEqual(f.read_text(), "The user replaced this\n")

    def test_quick_reinstalls_never_overwrite_the_first_backup(self):
        for _ in range(3):
            self.relay("install-claude")
        backups = sorted(self.claude.glob("settings.json.relay-backup-*"))
        self.assertEqual(len(backups), 3)
        originals = [b for b in backups if json.loads(b.read_text()) == self.user_settings]
        self.assertEqual(len(originals), 1, "the pre-install settings survive in exactly one backup")

    def test_install_without_existing_settings(self):
        self.settings.unlink()
        r = self.relay("install-claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list(self.claude.glob("settings.json.relay-backup-*")), [])
        self.assertIn("Stop", self.read()["hooks"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
