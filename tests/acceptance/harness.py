"""Acceptance-test harness: drives real interactive Claude Code and Codex windows through tmux.

Runs on the dev box against the isolated environment built by devenv/setup.py: its own relay state,
reviewer Codex home, Claude configuration, throwaway repository and tmux server. Everything it does is
printed.
"""
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "devenv"))
import setup  # noqa: E402

# This process and every child see only the isolated locations, never the installed relay or the
# Claude session that launched the run.
isolated = setup.env()  # built from the current environment, so before it is cleared
os.environ.clear()
os.environ.update(isolated)
RELAY = str(ROOT / "bin" / "relay")
REPO = setup.REPO
RELAY_HOME = setup.RELAY_HOME
CODEX_HOME = str(setup.CODEX_HOME)
sys.path.insert(0, str(ROOT))
from ccrelay import store  # noqa: E402


def log(msg):
    print(f"[test {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd, check=True, env=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise RuntimeError(f"{cmd!r} failed: {r.stderr.strip()[:300]}")
    return r.stdout


class Failed(Exception):
    pass


def check(cond, what):
    if not cond:
        raise Failed(what)
    log(f"  ok: {what}")


RATE_LIMITS = []  # Codex back-offs a wait sat through; the run adds them to the test's evidence


def wait_until(fn, timeout, what, interval=1.0):
    """Poll `fn` until it is truthy. If Codex is waiting out a rate limit (HTTP 429) logged since the test
    began, the deadline moves to the end of that back-off plus the original timeout, since nothing can happen
    sooner, up to RATE_LIMIT_CAP after the wait began."""
    start = time.time()
    end = start + timeout
    while True:
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(interval)
        backoff = codex_backoff_end(TEST_STARTED or start)
        extended = min(backoff + timeout, start + RATE_LIMIT_CAP) if backoff else end
        if extended <= end:
            break
        end = extended
        RATE_LIMITS.append(f"{what}: Codex was rate-limited (429) until {time.strftime('%H:%M:%S', time.localtime(backoff))}")
        log(f"  {RATE_LIMITS[-1]}; waiting until {time.strftime('%H:%M:%S', time.localtime(end))}")
    raise Failed(f"timed out after {round(time.time() - start)}s waiting for: {what}")


RATE_LIMIT_CAP = 1800  # longest any single wait may stretch to while Codex sits out back-offs
TEST_STARTED = None  # set by reset(); back-offs logged since then count
RETRY_RE = re.compile(r"retrying sampling request \(\d+/\d+ in ([\d.]+)s\)")


def codex_backoff_end(since):
    """When the latest rate-limit back-off the development Codex started since `since` ends, or None. Read
    from the daemon's own log, read-only; an unreadable log means no back-off is known."""
    ends = []
    for path in pathlib.Path(CODEX_HOME).glob("logs_*.sqlite"):
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = c.execute("SELECT ts, feedback_log_body FROM logs WHERE ts >= ? AND target LIKE '%responses_retry'"
                                 " AND feedback_log_body LIKE '%429 Too Many Requests%'", (int(since),)).fetchall()
            finally:
                c.close()
        except sqlite3.Error:
            continue
        ends += [ts + float(m.group(1)) for ts, body in rows if (m := RETRY_RE.search(body or ""))]
    return max(ends, default=None)


# --- tmux -----------------------------------------------------------------

def tmux(*args, check=True):
    """tmux on the harness's own server (socket setup.TMUX_SOCKET, no user tmux.conf)."""
    r = subprocess.run(["tmux", "-L", setup.TMUX_SOCKET, "-f", "/dev/null", *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"tmux {' '.join(args)!r} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def tmux_start(name, cmd, cwd=REPO, env=None):
    """A window running `cmd` (exec'd, so the pane's pid is the program's) with the isolated environment."""
    pinned = {k: os.environ[k] for k in ("RELAY_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")}
    flags = [a for k, v in {**pinned, **(env or {})}.items() for a in ("-e", f"{k}={v}")]
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "60", "-c", str(cwd), *flags, f"exec {cmd}")


def sessions():
    return tmux("ls", "-F", "#S", check=False).split()


def pane_pid(name):
    return int(tmux("display-message", "-p", "-t", name, "#{pane_pid}").strip())


def keys(name, *k):
    tmux("send-keys", "-t", name, *k)


def type_line(name, text):
    tmux("send-keys", "-t", name, "-l", text)
    time.sleep(0.5)
    keys(name, "Enter")


def screen(name):
    return tmux("capture-pane", "-p", "-t", name, check=False)


def kill_session(name):
    tmux("kill-session", "-t", name, check=False)


# --- relay state ----------------------------------------------------------

def q(sql, *args):
    c = store.connect()  # creates the schema on a fresh state directory
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def handoffs(label):
    return q("SELECT * FROM handoffs WHERE label=? ORDER BY id", label)


def latest(label):
    hs = handoffs(label)
    return hs[-1] if hs else None


def drafts(hid):
    return q("SELECT * FROM drafts WHERE handoff_id=? ORDER BY id", hid)


def events(hid=None):
    if hid is None:
        return q("SELECT * FROM events ORDER BY id")
    return q("SELECT * FROM events WHERE handoff_id=? ORDER BY id", hid)


def marks(since):
    """(session IDs, labels) of the run's tests. The development store holds only the latest test's, so call this
    before the next reset clears it. The transcripts of Claude sessions active since `since` add every test
    session's ID, including one only the installed hooks saw, which never reaches the development store."""
    sids = {r["session_id"] for t in ("links", "handoffs", "sessions") for r in q(f"SELECT session_id FROM {t}")}
    sids |= {p.stem for p in (setup.CLAUDE_CONFIG_DIR / "projects").glob("*/*.jsonl") if p.stat().st_mtime >= since}
    labels = {r["label"] for t in ("links", "threads", "handoffs") for r in q(f"SELECT label FROM {t}")}
    return sids, labels


def notifications():
    p = RELAY_HOME / "notifications.log"
    return p.read_text() if p.exists() else ""


def thread_id(label):
    rows = q("SELECT thread_id FROM threads WHERE label=?", label)
    return rows[0]["thread_id"] if rows else None


# --- Claude ---------------------------------------------------------------

def claude_start(name, label=None, env=None, role=None, cwd=REPO, mode=None):
    """A Claude window, linked to `label` (as `role`, if given) when a label is given; returns its session ID.
    `mode` overrides the configuration's auto mode, e.g. "default" for a session that asks for permissions."""
    tmux_start(name, "claude --model haiku" + (f" --permission-mode {mode}" if mode else ""), cwd=cwd, env=env)
    wait_until(lambda: "❯" in screen(name) and (("auto mode" in screen(name)) == (mode in (None, "auto"))), 60,
               f"{name} prompt in {mode or 'auto'} mode")
    if label:
        before = {r["session_id"] for r in q("SELECT session_id FROM links")}

        def mine():  # this window's link: other sessions may already be linked to the same label
            return [r for r in q("SELECT session_id, skip_next FROM links WHERE label=?", label)
                    if r["session_id"] not in before]
        type_line(name, "/relay-link " + (f"--as {role} " if role else "") + label)
        wait_until(mine, 60, f"{name} linked to {label}")
        # the /relay-link turn's own Stop hook clears skip_next; that is the relay's signal the turn ended
        wait_until(lambda: mine()[0]["skip_next"] == 0, 90, f"{name} /relay-link turn finished")
        wait_until(lambda: "esc to interrupt" not in screen(name), 30, f"{name} idle")
        return mine()[0]["session_id"]


def trust_claude_folder(path):
    """Mark another folder (a worktree, say) as trusted in the test Claude configuration, as setup does for REPO."""
    state = setup.CLAUDE_CONFIG_DIR / ".claude.json"
    d = json.loads(state.read_text())
    d.setdefault("projects", {}).setdefault(str(pathlib.Path(path).resolve()), {}).update(
        {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True})
    state.write_text(json.dumps(d, indent=2))


def session_handoff(sid, status, timeout=240, after_id=0):
    """The newest hand-off from session `sid`, once it is past `after_id` and in `status`."""
    def ready():
        rows = q("SELECT * FROM handoffs WHERE session_id=? AND id>? ORDER BY id DESC LIMIT 1", sid, after_id)
        if rows and rows[0]["status"] in (status if isinstance(status, tuple) else (status,)):
            return rows[0]
    return wait_until(ready, timeout, f"session {sid[:8]} handoff {status}")


def claude_say(name, text):
    type_line(name, text)


def feedback_entries(transcript_path):
    out = []
    with open(transcript_path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = (e.get("message") or {}).get("content")
            if e.get("type") == "user" and isinstance(c, str) and c.startswith("Stop hook feedback:\n"):
                out.append(c[len("Stop hook feedback:\n"):])
    return out


def wait_handoff(label, status, timeout=240, after_id=0):
    def ready():
        h = latest(label)
        if h and h["id"] > after_id and h["status"] in (status if isinstance(status, tuple) else (status,)):
            return h
    return wait_until(ready, timeout, f"{label} handoff {status}")


# --- Codex ----------------------------------------------------------------

MENU_RE = re.compile(r"^\s*(›)?\s*(\d)\.\s+(.+?)\s*$")


def codex_open(name, label):
    WINDOW_LABEL[name] = label
    tmux_start(name, f"{RELAY} review {label}")
    wait_until(lambda: "OpenAI Codex" in screen(name) or "›" in screen(name), 60, f"{name} Codex window")


def menu_options(name):
    s = screen(name)
    if "enter to submit" not in s or "Choice" not in s:
        return None
    opts = []
    for line in s[s.rindex("Choice"):].splitlines():
        m = MENU_RE.match(line)
        if m:
            opts.append((m.group(3), bool(m.group(1))))
    return opts or None


def wait_menu(name, timeout=240):
    return wait_until(lambda: menu_options(name), timeout, f"{name} menu")


def choose(name, choice):
    """Pick the first menu option that starts with `choice`: "Send" is the first Send to …, the reporting session."""
    opts = wait_menu(name)
    labels = [o[0] for o in opts]
    target = next((i for i, label in enumerate(labels) if label.startswith(choice)), None)
    if target is None:
        raise Failed(f"menu has no {choice!r}: {labels}")
    cur = next(i for i, o in enumerate(opts) if o[1])
    for _ in range(target - cur):
        keys(name, "Down")
    for _ in range(cur - target):
        keys(name, "Up")
    time.sleep(0.5)
    keys(name, "Enter")
    log(f"  chose {labels[target]!r} in {name} (options {labels})")
    return labels


WINDOW_LABEL = {}


def ask_codex(name, text):
    """Type into the Codex window, as the user would, once Codex has finished its turn."""
    wait_until(lambda: menu_options(name) is None, 30, f"{name} composer free")
    label = WINDOW_LABEL.get(name)
    if label:
        wait_until(lambda: codex_idle(label), 180, f"{name} Codex idle")
    type_line(name, text)


MENU_RETRIES = []


def current_handoff(name):
    """`H<n>` for the latest handoff of the ticket open in this Codex window; the relay tools need it."""
    h = latest(WINDOW_LABEL[name])
    return f"H{h['id']}"


def ask_menu(name, attempts=3, hid=None):
    """Ask Codex for the menu (of hand-off `hid`, else the ticket's newest) as the user would; record when the
    model needed asking more than once."""
    for i in range(1, attempts + 1):
        ask_codex(name, f"Call the relay show_menu tool now with handoff {f'H{hid}' if hid else current_handoff(name)}. "
                        "Do not reply with text first.")
        try:
            opts = wait_until(lambda: menu_options(name), 90, f"{name} menu (ask {i})")
            if i > 1:
                MENU_RETRIES.append(f"{name}: menu shown after {i} asks")
            return opts
        except Failed:
            if WINDOW_LABEL.get(name):
                wait_until(lambda: codex_idle(WINDOW_LABEL[name]), 180, f"{name} Codex idle")
    raise Failed(f"Codex did not call show_menu after {attempts} asks")


def inject_draft(label, text, hid=None):
    """Store a known draft for hand-off `hid` (else the label's newest), exactly as propose_reply would."""
    from ccrelay import mcp_server
    conn = store.connect()
    h = store.handoff(conn, hid) if hid else store.latest_handoff_for_thread(conn, thread_id(label))
    mcp_server.save_draft(conn, store.load_config(), h, text)
    return store.latest_draft(conn, h["id"])


def thread_turns(tid):
    """The conversation's turns as the development Codex daemon reports them (thread/read with turns)."""
    from ccrelay import codexrpc
    result = codexrpc.call(store.load_config(), "thread/read", {"threadId": tid, "includeTurns": True}) or {}
    return (result.get("thread") or {}).get("turns") or []


def turn_of(turns, hid):
    """Index of the turn whose input carries hand-off H<hid>'s header, or None."""
    marker = f"[cc-codex-relay handoff H{hid} · ticket"
    return next((i for i, t in enumerate(turns) if marker in json.dumps(t, ensure_ascii=False)), None)


def rollout(tid):
    """The development Codex home's recorded session file for thread `tid` (it holds the thread's instructions)."""
    paths = sorted(pathlib.Path(CODEX_HOME, "sessions").glob(f"**/*{tid}*.jsonl"))
    return "".join(p.read_text() for p in paths)


CONTINUE = setup.TMP / "continue"


def human_look(what, windows, timeout=1800):
    """With ACCEPT_HUMAN=1, hold the test while a person looks at `windows` (read-only tmux attach) and
    then writes what they saw to CONTINUE. Without it, the look is skipped and the evidence says so."""
    if not os.environ.get("ACCEPT_HUMAN"):
        return f"human look skipped (ACCEPT_HUMAN unset): {what}"
    CONTINUE.unlink(missing_ok=True)
    log(f"HUMAN: look at {what}: " + "; ".join(f"tmux -L {setup.TMUX_SOCKET} attach -r -t {w}" for w in windows)
        + f"; then write what you saw to {CONTINUE}")
    try:
        wait_until(CONTINUE.exists, timeout, f"human look: {what}", interval=2)
    except Failed:
        return f"INCONCLUSIVE: human look timed out: {what}"
    time.sleep(1)
    seen = CONTINUE.read_text().strip()
    CONTINUE.unlink()
    return f"human look at {what}: {seen or 'seen'}"


# --- environment ----------------------------------------------------------

def reset():
    """Fresh relay state and throwaway repo, with the development Codex daemon stopped; the sign-ins are kept."""
    global TEST_STARTED
    TEST_STARTED = time.time()
    log("reset: killing test windows, clearing relay state and the throwaway repo")
    tmux("kill-server", check=False)  # the harness's own tmux server only
    subprocess.run(["pkill", "-f", f"{RELAY} hook-stop"])  # the development relay's hooks only
    stop_daemon()
    setup.clear_test_state()
    setup.setup()


def stop_daemon():
    """Stop the development Codex daemon, so a turn still running from the previous test (one held back by a
    rate limit, say) can't act on the next test's store. The relay starts it again on the next hand-off."""
    from ccrelay import codexrpc
    cfg = store.load_config()
    try:
        subprocess.run([cfg["codex_bin"], "app-server", "daemon", "stop"], env=codexrpc.codex_env(cfg),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        pass
    if daemon_pid():
        # a graceful stop waits for an open menu (DESIGN build findings): kill the development daemon only
        os.kill(daemon_pid(), signal.SIGKILL)
        log("  development Codex daemon did not stop within 15s; killed it")
    wait_until(lambda: daemon_pid() is None, 15, "development Codex daemon stopped")


DAEMON_RE = re.compile("^" + re.escape(CODEX_HOME) + r"/packages/app-server-daemon/releases/[^/]+/bin/codex app-server --listen")


def daemon_pid():
    """The development Codex home's daemon, matched by its path; never the installed reviewer's daemon."""
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except OSError:
            continue
        if DAEMON_RE.match(cmdline) and pid_alive(int(proc.name)):
            return int(proc.name)
    return None


def pid_alive(pid):
    from ccrelay.claude_hooks import pid_alive as relay_pid_alive  # treats zombies as gone
    return relay_pid_alive(pid)


def codex_idle(label):
    from ccrelay import codexrpc
    try:
        return codexrpc.thread_status(store.load_config(), thread_id(label)) == "idle"
    except Exception:
        return False
