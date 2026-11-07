"""Claude Code hook entry points: Stop (file the handoff, then wait for the user's decision) and SessionEnd."""
import json
import os
import signal
import sys
import time

from . import codexrpc, intake, store
from .notify import notify

FEEDBACK_PREFIX = "Stop hook feedback:\n"
REQUIRED_STOP_FIELDS = ("session_id", "transcript_path", "last_assistant_message")


def transcript_entries(path):
    try:
        with open(path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def last_assistant_text(path):
    """Final assistant text in a Claude transcript (used by /relay-review to forward the previous turn)."""
    last = None
    for entry in transcript_entries(path):
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        parts = [c.get("text", "") for c in content if c.get("type") == "text"] if isinstance(content, list) else []
        if parts:
            last = "\n".join(parts)
    return last


def transcript_has_feedback(path, text):
    """True if Claude's transcript shows `text` delivered as Stop-hook feedback."""
    wanted = FEEDBACK_PREFIX + text
    for entry in transcript_entries(path):
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user" and content == wanted:
            return True
    return False


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def claude_process_pid():
    """Pid of the Claude Code process running this hook (CLAUDE_PID, else the nearest `claude` ancestor)."""
    env_pid = os.environ.get("CLAUDE_PID")
    if env_pid and env_pid.isdigit():
        return int(env_pid)
    pid = os.getppid()
    for _ in range(4):
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().split(b"\0")[0]
        except OSError:
            return None
        if cmd.endswith(b"claude") or b"claude-code" in cmd:
            return pid
        pid = int(fields[1])
    return None


def file_handoff(db, sid, link, message, transcript_path, cwd):
    """Record a queued hand-off for a linked session, with who sent it and the folder it is in now (`cwd`);
    branch and HEAD are read from that same folder."""
    branch, head = store.git_state(cwd)
    t = store.now()
    hid = db.execute(
        "INSERT INTO handoffs (session_id, label, display_name, cwd, branch, head, transcript_path, claude_message,"
        " status, hook_pid, recipient_aware, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 1, ?, ?)",
        (sid, link["label"], store.display_name(link["role"], link["cwd"], sid), cwd, branch, head,
         transcript_path, message, os.getpid(), t, t)).lastrowid
    store.event(db, "handoff:queued", link["label"], hid)
    return hid


def stop():
    raw = sys.stdin.read()
    db = store.connect()
    cfg = store.load_config()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    missing = [k for k in REQUIRED_STOP_FIELDS if not isinstance(payload, dict) or k not in payload]
    if missing:
        store.event(db, "hook:unsupported_payload", f"missing {missing}")
        notify(db, "Relay: Claude hook incompatible",
               f"Stop hook payload is missing {', '.join(missing)}; nothing was forwarded.")
        return 0
    sid = payload["session_id"]
    link = db.execute("SELECT * FROM links WHERE session_id=?", (sid,)).fetchone()
    if not link:
        return 0
    if link["skip_next"]:
        db.execute("UPDATE links SET skip_next=0 WHERE session_id=?", (sid,))
        if link["once"] and not link["once_message"]:
            db.execute("DELETE FROM links WHERE session_id=?", (sid,))
        if not link["once_message"]:
            return 0
    if payload.get("background_tasks"):
        store.event(db, "hook:skipped_background", sid)
        return 0
    message = link["once_message"] or payload["last_assistant_message"] or ""
    if link["once"]:
        db.execute("DELETE FROM links WHERE session_id=?", (sid,))

    for old in db.execute("SELECT id FROM handoffs WHERE session_id=? AND status IN (%s)"
                          % ",".join("?" * len(store.WAITING)), (sid, *store.WAITING)).fetchall():
        store.set_handoff(db, old["id"], "superseded", "newer turn from the same session", only_from=store.WAITING)
    hid = file_handoff(db, sid, link, message, payload["transcript_path"], payload.get("cwd") or link["cwd"])
    h = store.handoff(db, hid)
    try:
        # The conversation, its persona and standing context belong to the project the session was linked in
        # (or, if that folder is gone, such as a removed worktree, the one it is in now); the folder it is in
        # now is otherwise only reported as context.
        home = link["cwd"] if os.path.isdir(link["cwd"]) else h["cwd"]
        thread_id = intake.ensure_thread(db, cfg, h["label"], home)
        db.execute("UPDATE handoffs SET thread_id=? WHERE id=?", (thread_id, hid))
        handover = intake.pending_handover(db, thread_id, home)
        text = intake.handoff_text(h, handover, store.last_delivered(db, sid))
        turn_running = codex_turn_running(cfg, thread_id)  # before queueing: this report may start a turn itself
        item = intake.queue(db, cfg, thread_id, text, f"relay-H{hid}")
        db.execute("UPDATE handoffs SET queue_item_id=? WHERE id=?", (item, hid))
        if handover:
            intake.mark_handover_sent(db, thread_id, handover["sha256"])
    except (OSError, codexrpc.CodexError) as e:
        store.set_handoff(db, hid, "failed", f"could not start the Codex assessment: {e}", only_from=("queued",))
        notify(db, f"{store.about(db, h)}: assessment failed", f"{e} — Claude was released; nothing was sent.", hid)
        return 0
    store.set_handoff(db, hid, "assessing", only_from=("queued",))
    tell_if_queued_behind_a_menu(db, store.handoff(db, hid), thread_id, turn_running)  # now with its conversation
    return wait(db, cfg, hid)


def codex_turn_running(cfg, thread_id):
    """Whether Codex has a turn running in the conversation; if it can't tell, assume so."""
    try:
        return codexrpc.thread_status(cfg, thread_id) != "idle"
    except (OSError, codexrpc.CodexError):
        return True


def tell_if_queued_behind_a_menu(db, h, thread_id, turn_running):
    """The report is in Codex's queue behind an open menu: say so (Q7). The menu is never redrawn. A menu can
    only be open inside a running turn, so a record left while Codex was idle is stale and is dropped. Only a
    notice: failing to give it never affects the report, which is already queued."""
    menu = store.open_menu(db, thread_id)
    if not menu:
        return
    if not turn_running:
        store.clear_menus(db, thread_id)
        return
    if not pid_alive(menu["pid"]):
        return
    try:
        notify(db, f"{store.about(db, h)} queued behind the open menu",
               f"It runs when you answer the menu for H{menu['handoff_id']}. Discuss lets it run first.", h["id"])
    except OSError as e:
        store.event(db, "notify_failed", str(e), h["id"])


def wait(db, cfg, hid):
    """Hold Claude's turn until the user sends a draft, finishes, or the wait limit passes."""
    def released(signum, _frame):
        store.set_handoff(db, hid, "released", "Claude released the wait (Esc or exit)", only_from=store.WAITING)
        os._exit(0)
    signal.signal(signal.SIGTERM, released)
    signal.signal(signal.SIGINT, released)
    signal.signal(signal.SIGHUP, released)

    claude_pid = claude_process_pid()
    deadline = time.time() + cfg["hook_wait_secs"]
    next_check = time.time() + 5
    pause = float(os.environ.get("RELAY_TEST_PAUSE_BEFORE_OUTPUT", "0"))
    while time.time() < deadline:
        h = store.handoff(db, hid)
        if h["status"] not in store.WAITING:
            return 0
        # only a draft the user addressed to this session, whichever report it answers
        draft = next(iter(store.addressed_to(db, hid, "approved")), None)
        if draft and store.set_draft(db, draft["id"], "sending", only_from=("approved",)):
            if pause:
                time.sleep(pause)
            sys.stdout.write(json.dumps({"decision": "block", "reason": draft["text"]}))
            sys.stdout.flush()
            store.delivered(db, draft["id"], hid)
            return 0
        if time.time() >= next_check:
            next_check = time.time() + 5
            if claude_pid and not pid_alive(claude_pid):
                store.set_handoff(db, hid, "released", "Claude exited while waiting", only_from=store.WAITING)
                return 0
            if h["status"] == "assessing":
                check_no_draft(db, cfg, h)
        time.sleep(0.5)
    store.set_handoff(db, hid, "timed_out", "Claude's wait limit passed; nothing was sent", only_from=store.WAITING)
    notify(db, f"{store.about(db, store.handoff(db, hid))}: timed out", "Claude was released; the draft was not sent.", hid)
    return 0


def check_no_draft(db, cfg, h):
    """If Codex finished the queued turn without saving a draft, say so once."""
    if store.latest_draft(db, h["id"]):
        return
    try:
        if h["queue_item_id"] in codexrpc.queued_ids(cfg, h["thread_id"]):
            return
        if codexrpc.thread_status(cfg, h["thread_id"]) != "idle":
            return
    except (OSError, codexrpc.CodexError):
        return
    if store.set_handoff(db, h["id"], "no_draft", "Codex finished without a draft", only_from=("assessing",)):
        notify(db, f"{store.about(db, h)}: assessment finished without a draft",
               "Open the Codex conversation to see why. Claude is still waiting.", h["id"])


def session_end():
    raw = sys.stdin.read()
    db = store.connect()
    cfg = store.load_config()
    try:
        sid = json.loads(raw)["session_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return 0
    db.execute("INSERT OR REPLACE INTO sessions (session_id, ended) VALUES (?, ?)", (sid, store.now()))
    open_states = store.WAITING + ("released", "timed_out")
    for h in db.execute("SELECT * FROM handoffs WHERE session_id=? AND status IN (%s)"
                        % ",".join("?" * len(open_states)), (sid, *open_states)).fetchall():
        if not store.set_handoff(db, h["id"], "session_ended", "the originating Claude session ended",
                                 only_from=open_states):
            continue  # another process settled it first (e.g. a send confirmed): "not sent" would be untrue
        told = False
        if h["thread_id"]:
            try:
                codexrpc.ensure_daemon(cfg)
                intake.queue(db, cfg, h["thread_id"],
                             f"[cc-codex-relay notice · handoff H{h['id']} · ticket {h['label']} · from "
                             f"{store.session_name(h)}]\n"
                             + intake.SESSION_ENDED_NOTICE, f"relay-H{h['id']}-ended")
                told = True
            except (OSError, codexrpc.CodexError) as e:
                store.event(db, "notice_failed", str(e), h["id"])
        notify(db, f"{store.about(db, h)}: Claude session ended",
               "The draft was not sent." + ("" if told else " Codex could not be told; open the conversation to review."),
               h["id"])
    db.execute("DELETE FROM links WHERE session_id=?", (sid,))
    return 0
