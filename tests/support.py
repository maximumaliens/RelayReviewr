"""Shared setup for the relay's unit checks: temporary relay state, temporary git projects, seeded
hand-offs and drafts, a stubbed menu form, and the real waiting hook run in a child process."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ccrelay import mcp_server, store  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


class StubServer:
    """Stands in for mcp_server.Server at its form method: records each form and returns scripted choices."""

    def __init__(self, *choices, while_open=None):
        self.choices = list(choices)
        self.forms = []
        self.while_open = while_open  # called while the first form is open, before the user answers

    def elicit(self, message, choices, abort=None):
        """The scripted choice is the first offered option that starts with it ("Send" picks the first Send
        to …); an option that isn't offered is returned as given, as a stray answer would be."""
        self.forms.append({"message": message, "choices": list(choices)})
        if self.while_open and len(self.forms) == 1:
            self.while_open()
            if abort and abort():  # the real server polls this while the form is open
                return mcp_server.ABORTED
        want = self.choices.pop(0) if self.choices else "Keep waiting"
        return next((c for c in choices if c.startswith(want)), want)


class FakeCodex:
    """Stands in for the Codex daemon at the relay's client (codexrpc). Like Codex, it runs one turn per
    conversation: a message queued while a turn is running waits in that conversation's queue; otherwise it
    starts a turn at once. `busy` holds the conversations with a turn running (a menu is only ever open inside
    one). `fail` makes the daemon unreachable. `instructions` and `loaded` record each new conversation's
    developer instructions and each conversation confirmed loaded."""

    def __init__(self, db):
        self.db = db
        self.busy, self.queues, self.texts, self.fail = set(), {}, [], None
        self.instructions, self.loaded = [], []

    def start_thread(self, cfg, cwd, instructions, name):
        self.instructions.append(instructions)
        return "thread-1" if not self.db.execute("SELECT 1 FROM threads").fetchone() else f"thread-{name}-{cwd}"

    def queue_message(self, cfg, thread_id, text, client_id):
        self.texts.append(text)
        item_id = f"item-{len(self.texts)}"  # like Codex: its own id, never the client message id
        if thread_id in self.busy:
            self.queues.setdefault(thread_id, []).append(item_id)
        self.busy.add(thread_id)
        return item_id

    def ensure_daemon(self, cfg):
        if self.fail:
            raise self.fail

    @contextlib.contextmanager
    def installed(self):
        from ccrelay import codexrpc
        with mock.patch.object(codexrpc, "ensure_daemon", self.ensure_daemon), \
                mock.patch.object(codexrpc, "ensure_loaded", lambda cfg, t: self.loaded.append(t)), \
                mock.patch.object(codexrpc, "start_thread", self.start_thread), \
                mock.patch.object(codexrpc, "queue_message", self.queue_message), \
                mock.patch.object(codexrpc, "queued_ids", lambda cfg, t: set(self.queues.get(t, []))), \
                mock.patch.object(codexrpc, "thread_status", lambda cfg, t: "active" if t in self.busy else "idle"):
            yield


class RelayCase(unittest.TestCase):
    """A temporary relay state folder per test, plus helpers to seed what the Stop hook and Codex leave."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.old_home = os.environ.get("RELAY_HOME")
        os.environ["RELAY_HOME"] = str(self.root / "state")
        self.db = store.connect()
        self.children = []

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait()
        self.db.close()
        if self.old_home is None:
            os.environ.pop("RELAY_HOME", None)
        else:
            os.environ["RELAY_HOME"] = self.old_home
        self.tmp.cleanup()

    def project(self, name="Project"):
        repo = self.root / name
        repo.mkdir()
        git("init", "-q", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "seed", cwd=repo)
        return repo

    def handoff(self, label="T-1", thread="thread-1", status="review_ready", session="abcdef0123456789",
                cwd=None, hook_pid=None, transcript=None, report="report"):
        t = store.now()
        return self.db.execute(
            "INSERT INTO handoffs (session_id, label, thread_id, cwd, transcript_path, claude_message, status,"
            " hook_pid, recipient_aware, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (session, label, thread, str(cwd or self.root), str(transcript or self.root / "t.jsonl"), report, status,
             hook_pid or os.getpid(), t, t)).lastrowid

    def draft(self, hid, text="do the thing", status="ready"):
        t = store.now()
        return self.db.execute(
            "INSERT INTO drafts (handoff_id, text, path, status, created, updated) VALUES (?, ?, '', ?, ?, ?)",
            (hid, text, status, t, t)).lastrowid

    def call(self, server, name, args, thread="thread-1"):
        """A Codex tool call, as the menu server's entry point receives it."""
        return mcp_server.tool(server, name, args, thread)

    def start_wait_hook(self, hid, claude_pid=None, pause_before_output=0):
        """Run the real waiting loop for hand-off `hid` in a child process; its stdout is what Claude receives.
        `pause_before_output` holds it between claiming a draft and printing it, so a test can kill it mid-send."""
        env = dict(os.environ, CLAUDE_PID=str(claude_pid or os.getpid()), PYTHONPATH=str(ROOT),
                   RELAY_TEST_PAUSE_BEFORE_OUTPUT=str(pause_before_output))
        code = ("from ccrelay import claude_hooks, store; "
                "import sys; sys.exit(claude_hooks.wait(store.connect(), store.load_config(), int(sys.argv[1])))")
        p = subprocess.Popen([sys.executable, "-c", code, str(hid)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(p)
        return p

    def run_stop_hook(self, session, label="T-1", linked_in=None, role=None, message="report", **payload):
        """Run the real Stop hook for a linked session against self.codex (a FakeCodex), with the waiting loop
        returning at once. Returns the hand-off it filed."""
        from ccrelay import claude_hooks
        linked_in = linked_in or self.root
        self.db.execute("INSERT OR REPLACE INTO links (session_id, label, cwd, role, created) VALUES (?, ?, ?, ?, 0)",
                        (session, label, str(linked_in), role))
        payload = {"session_id": session, "transcript_path": str(self.root / f"{session}.jsonl"),
                   "last_assistant_message": message, **payload}
        with self.codex.installed(), mock.patch.object(claude_hooks, "wait", lambda db, cfg, hid: 0), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            claude_hooks.stop()
        return self.db.execute("SELECT * FROM handoffs WHERE session_id=? ORDER BY id DESC LIMIT 1",
                               (session,)).fetchone()

    def run_link(self, session, cwd, *args, once=False):
        """Run the command /relay-link (or /relay-review, with `once`) runs, in Claude session `session` started
        in `cwd`, against self.codex; returns what it printed. HOME is the test's folder, where /relay-review
        reads the session's transcript (cli.transcript_path)."""
        from ccrelay import cli
        out = io.StringIO()
        env = {"CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PROJECT_DIR": str(cwd), "HOME": str(self.root)}
        with mock.patch.dict(os.environ, env), self.codex.installed(), contextlib.redirect_stdout(out):
            cli.link(list(args), once=once)
        return out.getvalue()

    @property
    def codex(self):
        if not hasattr(self, "_codex"):
            self._codex = FakeCodex(self.db)
        return self._codex

    def hook_output(self, p, timeout=20):
        out, _ = p.communicate(timeout=timeout)
        return json.loads(out) if out.strip() else None
