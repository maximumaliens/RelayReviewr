"""The isolated acceptance setup keeps only the current checkout's relay hooks in its own Claude configuration,
whichever checkout an earlier setup left hooks for. Everything here lives in temporary folders."""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


class RefreshAcrossCheckouts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        env = {"RELAY_ACCEPT_ROOT": str(root / "accept"), "RELAY_ACCEPT_TMP": str(root / "tmp")}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("isolated_setup", ROOT / "devenv" / "setup.py")
            self.setup = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.setup)
        self.config = self.setup.CLAUDE_CONFIG_DIR
        self.config.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def commands(self, settings, event):
        return [h["command"] for g in settings["hooks"][event] for h in g["hooks"]]

    def test_hooks_left_by_another_checkout_are_replaced_and_the_rest_is_kept(self):
        old = "/somewhere/older-checkout/bin/relay"
        (self.config / ".credentials.json").write_text('{"token": "keep"}')
        (self.config / "settings.json").write_text(json.dumps({
            "env": {"KEEP": "1"}, "model": "kept-model",
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": f"{old} hook-stop", "timeout": 14400}]},
                         {"hooks": [{"type": "command", "command": "echo unrelated", "timeout": 5}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": f"{old} hook-session-end", "timeout": 5}]}],
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo other-event"}]}]}}))
        for _ in range(2):  # a second refresh changes nothing
            self.setup.write_claude_config()
            settings = json.loads((self.config / "settings.json").read_text())
            current = str(ROOT / "bin" / "relay")
            self.assertEqual(self.commands(settings, "Stop"), ["echo unrelated", f"{current} hook-stop"])
            self.assertEqual(self.commands(settings, "SessionEnd"), [f"{current} hook-session-end"])
            self.assertEqual(self.commands(settings, "PreToolUse"), ["echo other-event"])
            self.assertEqual(settings["model"], "kept-model")
            self.assertEqual(settings["env"]["KEEP"], "1")
            self.assertEqual(settings["permissions"]["defaultMode"], "auto")
            self.assertEqual((self.config / ".credentials.json").read_text(), '{"token": "keep"}')

    def test_a_first_setup_with_no_settings_installs_the_current_hooks_only(self):
        self.setup.write_claude_config()
        settings = json.loads((self.config / "settings.json").read_text())
        current = str(ROOT / "bin" / "relay")
        self.assertEqual(self.commands(settings, "Stop"), [f"{current} hook-stop"])
        self.assertEqual(self.commands(settings, "SessionEnd"), [f"{current} hook-session-end"])


if __name__ == "__main__":
    unittest.main()
