"""Unit checks for the toast environment and the interpreter choice in install-codex."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccrelay import store  # noqa: E402
from ccrelay.notify import notify, toast_env  # noqa: E402
from support import RelayCase  # noqa: E402


class ToastEnv(unittest.TestCase):
    def test_uses_w_flag_and_keeps_existing_wslenv(self):
        env = toast_env("T", "B", base={"WSLENV": "USERPROFILE/p"})
        self.assertEqual(env["WSLENV"], "USERPROFILE/p:RELAY_TITLE/w:RELAY_BODY/w")
        self.assertEqual((env["RELAY_TITLE"], env["RELAY_BODY"]), ("T", "B"))

    def test_without_existing_wslenv(self):
        self.assertEqual(toast_env("T", "B", base={})["WSLENV"], "RELAY_TITLE/w:RELAY_BODY/w")


class TitlePrefix(RelayCase):
    """A relay state whose config sets notify_title_prefix (the acceptance environment's) marks every title."""

    def notified(self):
        notify(self.db, "T-1: review ready", "body")
        return (store.relay_home() / "notifications.log").read_text()

    def test_prefix_from_config_marks_the_title(self):
        (store.relay_home() / "config.json").write_text('{"notify_title_prefix": "[relay test] "}')
        self.assertIn(" [relay test] T-1: review ready: body", self.notified())

    def test_no_prefix_by_default(self):
        self.assertIn(" T-1: review ready: body", self.notified())
        self.assertNotIn("[relay test]", self.notified())


class InterpreterChoice(unittest.TestCase):
    def test_install_codex_from_a_venv_records_the_base_python(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            venv = pathlib.Path(tmp) / "venv"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
            home = pathlib.Path(tmp) / "codex"
            state = pathlib.Path(tmp) / "state"
            state.mkdir()
            (state / "config.json").write_text(f'{{"codex_home": "{home}"}}')
            subprocess.run([str(venv / "bin" / "python3"), str(root / "bin" / "relay"), "install-codex"],
                           env={"RELAY_HOME": str(state), "PATH": "/usr/bin:/bin"}, check=True, capture_output=True)
            text = (home / "config.toml").read_text()
            self.assertNotIn(str(venv), text, "the venv interpreter must not be recorded")
            self.assertIn('command = "/', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
