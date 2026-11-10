"""Unit tests never reach the real clipboard or Windows toasts (#9): RelayCase hides powershell.exe and clip.exe
from the test and from every process it starts, and fails a test that launches either. Copy tests opt in to a fake."""
import os
import shutil
import subprocess
import sys

from ccrelay import mcp_server, notify, store
from support import WINDOWS_TOOLS, LaunchedWindowsTool, RelayCase, path_without_windows_tools

FIND = "import shutil, sys; print([t for t in sys.argv[1:] if shutil.which(t)])"


class NoWindowsTools(RelayCase):
    def test_neither_tool_is_found_in_the_test_process(self):
        self.assertEqual([t for t in WINDOWS_TOOLS if shutil.which(t)], [])

    def test_neither_tool_is_found_in_a_process_a_test_starts(self):
        out = subprocess.run([sys.executable, "-c", FIND, *WINDOWS_TOOLS], env=dict(os.environ),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_launching_either_tool_fails_the_test(self):
        for tool in WINDOWS_TOOLS:
            with self.subTest(tool=tool), self.assertRaises(LaunchedWindowsTool):
                subprocess.Popen(["/mnt/c/anywhere/" + tool])
            with self.subTest(tool=tool, via="run"), self.assertRaises(LaunchedWindowsTool):
                subprocess.run([tool])

    def test_a_notice_is_logged_and_toasts_nothing(self):
        notify.notify(self.db, "T-1: review ready", "body")
        self.assertIn("T-1: review ready: body", (store.relay_home() / "notifications.log").read_text())

    def test_copy_without_a_fake_says_there_is_no_clipboard(self):
        d = {"id": 1, "text": "x", "path": "draft.txt"}
        self.assertTrue(mcp_server.copy_draft(self.db, d).startswith("No clipboard here."))


class FakeClipboard(RelayCase):
    def test_receives_what_copy_sends_in_this_process_and_in_a_child(self):
        copied = self.fake_clipboard()
        d = self.db.execute("INSERT INTO drafts (handoff_id, text, path, status, created, updated) "
                            "VALUES (0, 'fix it', '', 'ready', 0, 0)").lastrowid
        draft = {"id": d, "text": "fix it ✓", "path": ""}
        self.assertTrue(mcp_server.copy_draft(self.db, draft).startswith("Copied the draft"))
        self.assertEqual(copied.read_bytes().decode("utf-16-le"), "fix it ✓")
        out = subprocess.run([sys.executable, "-c", FIND, "clip.exe", "powershell.exe"], env=dict(os.environ),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "['clip.exe']", "the fake is the only tool a child can find")

    def test_the_fake_is_gone_for_the_next_test(self):
        self.assertIsNone(shutil.which("clip.exe"))


class PathFilter(RelayCase):
    def test_keeps_folders_without_the_tools_in_order(self):
        with_tool = self.root / "win"
        with_tool.mkdir()
        (with_tool / "clip.exe").write_text("")
        (with_tool / "clip.exe").chmod(0o755)
        path = os.pathsep.join(["/usr/bin", str(with_tool), "/bin"])
        self.assertEqual(path_without_windows_tools(path), "/usr/bin" + os.pathsep + "/bin")
