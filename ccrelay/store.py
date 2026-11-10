"""SQLite store for links, Codex threads, handoffs, drafts and the event log.

All state transitions that matter for "send at most once" are single conditional
UPDATEs, so two processes (the waiting Claude hook and the Codex menu server)
can never both act on the same draft.
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    session_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    cwd TEXT NOT NULL,
    once INTEGER NOT NULL DEFAULT 0,
    skip_next INTEGER NOT NULL DEFAULT 0,
    once_message TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    project TEXT NOT NULL,
    label TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (project, label)
);
CREATE TABLE IF NOT EXISTS handovers_sent (
    thread_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    sent REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    label TEXT NOT NULL,
    thread_id TEXT,
    cwd TEXT NOT NULL,
    transcript_path TEXT,
    claude_message TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    queue_item_id TEXT,
    hook_pid INTEGER,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL REFERENCES handoffs(id),
    text TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    suggested_recipient TEXT,
    recipient_handoff_id INTEGER,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source_handoff_id INTEGER,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    from_line INTEGER,
    to_line INTEGER
);
CREATE TABLE IF NOT EXISTS menus_open (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    handoff_id INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    opened REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS no_reply_marks (
    handoff_id INTEGER PRIMARY KEY REFERENCES handoffs(id),
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    ended REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    handoff_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

# Handoff states in which the originating Claude turn is (meant to be) held by the hook.
WAITING = ("queued", "assessing", "no_draft", "review_ready")


def addressee(alias=""):
    """SQL for the hand-off a draft is addressed to. A draft approved before recipients existed has none: it goes
    to the session whose report it answers, as it always did."""
    t = f"{alias}." if alias else ""
    return f"COALESCE({t}recipient_handoff_id, {t}handoff_id)"


def relay_home():
    return pathlib.Path(os.environ.get("RELAY_HOME", pathlib.Path.home() / ".local/state/cc-codex-relay"))


def load_config():
    path = relay_home() / "config.json"
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg.setdefault("codex_bin", "codex")
    cfg.setdefault("codex_home", str(pathlib.Path.home() / ".codex-reviewer"))
    cfg.setdefault("approval_policy", "on-request")
    cfg.setdefault("hook_wait_secs", 14340)
    cfg.setdefault("menu_inline_lines", 40)
    cfg.setdefault("menu_inline_chars", 3000)
    cfg.setdefault("menu_additions_chars", 1500)
    cfg.setdefault("send_pickup_secs", 15)
    cfg.setdefault("personas", {})
    return cfg


def main_checkout(cwd):
    """The main checkout of the git repository holding `cwd` (the same for every worktree), or None."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
    except OSError:
        return None
    common = pathlib.Path(r.stdout.strip()) if r.returncode == 0 else None
    return common.parent if common and common.name == ".git" else None


def project_of(cwd):
    """The project a folder belongs to: its repository's main checkout, or the folder itself outside a repository."""
    return str((main_checkout(cwd) or pathlib.Path(cwd)).resolve())


def migrate_threads(db):
    """One-off: threads were keyed by label alone. Rebuild the table keyed by project plus label, taking each
    existing conversation's project from its stored folder."""
    if "project" in {r["name"] for r in db.execute("PRAGMA table_info(threads)")}:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if "project" not in {r["name"] for r in db.execute("PRAGMA table_info(threads)")}:  # another process may have won
            rows = db.execute("SELECT label, thread_id, cwd, created FROM threads").fetchall()
            db.execute("DROP TABLE threads")
            db.execute("CREATE TABLE threads (project TEXT NOT NULL, label TEXT NOT NULL, thread_id TEXT NOT NULL,"
                       " cwd TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (project, label))")
            db.executemany("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, ?, ?, ?, ?)",
                           [(project_of(r["cwd"]), r["label"], r["thread_id"], r["cwd"], r["created"]) for r in rows])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def connect():
    home = relay_home()
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "relay.db", timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    migrate_threads(db)
    for table, column, kind in (("drafts", "suggested_recipient", "TEXT"), ("links", "role", "TEXT"),
                                ("handoffs", "display_name", "TEXT"), ("handoffs", "branch", "TEXT"),
                                ("handoffs", "head", "TEXT"), ("drafts", "recipient_handoff_id", "INTEGER"),
                                ("handoffs", "recipient_aware", "INTEGER"), ("links", "once_label", "TEXT"),
                                ("links", "once_role", "TEXT"), ("drafts", "note", "TEXT")):
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return db


def display_name(role, cwd, sid):
    """How a session is named to people: its role, else its folder plus a short session ID."""
    return role or f"{pathlib.Path(cwd).name}-{sid[:6]}"


def session_name(h):
    """How the session behind hand-off `h` is named; hand-offs filed before names were recorded get the fallback."""
    return h["display_name"] or display_name(None, h["cwd"], h["session_id"])


def about(db, h):
    """How a notification names the report it is about, e.g. "ABC-42: verifier report H7". Another held session
    on the same conversation with the same name gets the short session ID appended, as in the menu."""
    name = session_name(h)
    others = db.execute(f"SELECT * FROM handoffs WHERE thread_id=? AND session_id!=? AND status IN "
                        f"({','.join('?' * len(WAITING))})", (h["thread_id"], h["session_id"], *WAITING)).fetchall()
    if any(session_name(r) == name for r in others):
        name = f"{name} ({h['session_id'][:6]})"
    return f"{h['label']}: {name} report H{h['id']}"


def where(h):
    """Where a session was when it filed hand-off `h`: folder · branch · HEAD, as far as known."""
    return " · ".join(p for p in (h["cwd"], h["branch"] and f"branch {h['branch']}", h["head"] and f"HEAD {h['head']}") if p)


def unique_names(sessions, taken=()):
    """Names by session ID for (session ID, name) pairs; two sessions with the same name are told apart by short
    session ID, and so is one whose name is among `taken`, the names other sessions already go by. A session
    given twice keeps its first name."""
    base = {}
    for sid, name in sessions:
        base.setdefault(sid, name)
    shared = {n for n in base.values() if list(base.values()).count(n) > 1} | set(taken)
    return {sid: f"{n} ({sid[:6]})" if n in shared else n for sid, n in base.items()}


# How a report that stopped waiting is worded in a session's state; any other status is shown as recorded.
STOPPED = {"sent": "busy", "released": "released", "timed_out": "timed out", "finished": "finished", "failed": "failed"}
BUSY = "it received an instruction and is working"
SKIPPED = "Last turn skipped because background work was running"
NO_TURN = "no turn finished since linking"


def ticket_sessions(db, thread_id):
    """The sessions on a conversation's ticket, as recorded: the one list behind the hand-off header, the tool
    results and the menu (#4). First each held session (a linked one, or a one-off review's while its report is
    held), in the order they reported. Then every other linked session: an ongoing link (/relay-link) whose label
    is the conversation's and whose folder is in the conversation's project, in the order they linked. The label
    alone never connects two projects, and a one-off review adds no entry of its own.

    Each entry is a dict: session_id, name (unique in the list), base (the name before a short session ID was
    added to tell it apart), held, handoff (the held report; else the newest report since the session's current
    link, or None) and state, the words shown for a session that isn't held.
    Only reports and skipped turns since the current link count, so linking again starts from "no turn finished
    since linking"."""
    waiting = db.execute(f"SELECT * FROM handoffs WHERE thread_id=? AND status IN ({','.join('?' * len(WAITING))})"
                         " ORDER BY id", (thread_id, *WAITING)).fetchall()
    held = {r["session_id"]: r for r in waiting}  # a session's newest held report
    entries = [{"session_id": sid, "base": session_name(r), "held": True, "handoff": r, "state": "held"}
               for sid, r in sorted(held.items(), key=lambda kv: kv[1]["id"])]
    thread = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    links = db.execute("SELECT * FROM links WHERE label=? AND once=0 ORDER BY created, session_id",
                       (thread["label"],)).fetchall() if thread else []
    projects = {}
    for link in links:
        sid = link["session_id"]
        if link["cwd"] not in projects:  # one git call per folder
            projects[link["cwd"]] = project_of(link["cwd"])
        if sid in held or projects[link["cwd"]] != thread["project"]:
            continue
        newest = db.execute("SELECT * FROM handoffs WHERE thread_id=? AND session_id=? AND created>=? ORDER BY id DESC"
                            " LIMIT 1", (thread_id, sid, link["created"])).fetchone()
        if newest and newest["status"] == "session_ended":
            # The link decides who is listed. Ending a session removes its link, so this one was made afterwards
            # and the ended report predates it, whatever the clock recorded: nothing was filed since linking.
            newest = None
        since = newest["created"] if newest else link["created"]
        skipped = db.execute("SELECT 1 FROM events WHERE kind='hook:skipped_background' AND detail=? AND ts>? LIMIT 1",
                             (sid, since)).fetchone()
        if skipped:
            state = SKIPPED
        elif not newest:
            state = NO_TURN
        else:
            word = STOPPED.get(newest["status"], newest["status"])
            reason = BUSY if newest["status"] == "sent" else newest["reason"]
            state = f"{word}: {reason}" if reason else word
        entries.append({"session_id": sid, "base": session_name(newest) if newest else
                        display_name(link["role"], link["cwd"], sid), "held": False, "handoff": newest, "state": state})
    names = unique_names((e["session_id"], e["base"]) for e in entries)
    return [{**e, "name": names[e["session_id"]]} for e in entries]


def addressed_to(db, hid, status):
    """Drafts in `status` addressed to hand-off `hid`, newest first."""
    return db.execute(f"SELECT * FROM drafts WHERE {addressee()}=? AND status=? ORDER BY id DESC", (hid, status)).fetchall()


def last_delivered(db, session_id):
    """The last instruction this session received: (draft id, source hand-off), or None."""
    row = db.execute("SELECT d.id AS did, d.handoff_id AS source_id FROM drafts d JOIN handoffs r "
                     f"ON r.id = {addressee('d')} WHERE r.session_id=? AND d.status='sent' "
                     "ORDER BY d.updated DESC, d.id DESC LIMIT 1", (session_id,)).fetchone()
    return (row["did"], handoff(db, row["source_id"])) if row else None


def git_state(cwd):
    """(branch, short HEAD) of the repository holding `cwd`; None for each that can't be read (no repository,
    no commits, detached HEAD for the branch, git missing)."""
    def run(*args):
        try:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None
    return run("branch", "--show-current"), run("rev-parse", "--short", "HEAD")


def now():
    return time.time()


def event(db, kind, detail="", handoff_id=None):
    db.execute("INSERT INTO events (ts, handoff_id, kind, detail) VALUES (?, ?, ?, ?)",
               (now(), handoff_id, kind, detail))
    live = load_config().get("live_log")
    if live:
        try:
            with open(live, "a") as f:
                tag = f" H{handoff_id}" if handoff_id else ""
                f.write(f"[relay {time.strftime('%H:%M:%S')}{tag}] {kind} {detail}\n")
        except OSError:
            pass


def set_handoff(db, hid, status, reason=None, only_from=None):
    """Move a handoff to `status`. With `only_from`, only if it is currently in one of those states."""
    sql = "UPDATE handoffs SET status=?, reason=COALESCE(?, reason), updated=? WHERE id=?"
    args = [status, reason, now(), hid]
    if only_from:
        sql += f" AND status IN ({','.join('?' * len(only_from))})"
        args += list(only_from)
    changed = db.execute(sql, args).rowcount == 1
    if changed:
        event(db, f"handoff:{status}", reason or "", hid)
    return changed


def set_draft(db, did, status, reason=None, only_from=None):
    sql = "UPDATE drafts SET status=?, reason=COALESCE(?, reason), updated=? WHERE id=?"
    args = [status, reason, now(), did]
    if only_from:
        sql += f" AND status IN ({','.join('?' * len(only_from))})"
        args += list(only_from)
    changed = db.execute(sql, args).rowcount == 1
    if changed:
        row = db.execute("SELECT handoff_id FROM drafts WHERE id=?", (did,)).fetchone()
        event(db, f"draft:{status}", f"D{did} {reason or ''}".strip(), row["handoff_id"])
    return changed


def approve(db, did, recipient_hid):
    """The user chose Send to the session holding `recipient_hid`: address the draft to it and approve it, in one
    conditional step, so only that session's waiting hook can collect it. Refused while another draft is on its
    way to that session, since its hook collects only one."""
    changed = db.execute(
        "UPDATE drafts SET status='approved', recipient_handoff_id=?, reason='the user chose Send', updated=?"
        " WHERE id=? AND status IN ('ready', 'failed') AND NOT EXISTS (SELECT 1 FROM drafts o WHERE o.id!=?"
        f" AND o.status IN ('approved', 'sending') AND {addressee('o')}=?)",
        (recipient_hid, now(), did, did, recipient_hid)).rowcount == 1
    if changed:
        row = db.execute("SELECT handoff_id FROM drafts WHERE id=?", (did,)).fetchone()
        event(db, "draft:approved", f"D{did} the user chose Send to H{recipient_hid}", row["handoff_id"])
    return changed


def delivered(db, did, recipient_hid, reason=None):
    """Record that draft `did` reached the session holding `recipient_hid`. That session is answered, and its own
    earlier unsent draft is closed so it can't be sent later. Another source session keeps waiting."""
    if not set_draft(db, did, "sent", reason, only_from=("sending",)):
        return False
    set_handoff(db, recipient_hid, "sent", only_from=WAITING + ("released", "timed_out"))
    for row in db.execute("SELECT id FROM drafts WHERE handoff_id=? AND id!=? AND status IN ('ready', 'failed')",
                          (recipient_hid, did)).fetchall():
        set_draft(db, row["id"], "closed", f"not sent: the session received D{did} instead",
                  only_from=("ready", "failed"))
    return True


def no_reply(db, hid):
    """Whether Codex assessed hand-off `hid` as needing no reply. The marker applies only while the hand-off has
    no draft: a draft saved later supersedes it, and nothing needs clearing."""
    return db.execute("SELECT 1 FROM no_reply_marks WHERE handoff_id=?", (hid,)).fetchone() is not None


def mark_no_reply(db, hid):
    """Record that Codex assessed hand-off `hid` as needing no reply: the marker and the move to review_ready
    (only from assessing or no_draft, as a saved draft does) are written together. Refused, writing nothing,
    for a hand-off that isn't being assessed or already has a draft, so it can't revive a stopped hand-off
    or hide a draft. True only when the marker is new."""
    db.execute("BEGIN IMMEDIATE")
    try:
        h = handoff(db, hid)
        if h["status"] not in ("assessing", "no_draft") or latest_draft(db, hid):
            db.execute("ROLLBACK")
            return False
        new = db.execute("INSERT OR IGNORE INTO no_reply_marks (handoff_id, created) VALUES (?, ?)",
                         (hid, now())).rowcount == 1
        if new:
            event(db, "no_reply:assessed", "Codex assessed that Claude needs no reply", hid)
        set_handoff(db, hid, "review_ready", only_from=("assessing", "no_draft"))
        db.execute("COMMIT")
        return new
    except Exception:
        db.execute("ROLLBACK")
        raise


def menu_opened(db, thread_id, hid):
    """A menu tool call for hand-off `hid` is open in conversation `thread_id`, in this (menu server) process.
    Returns its record's id for menu_closed."""
    return db.execute("INSERT INTO menus_open (thread_id, handoff_id, pid, opened) VALUES (?, ?, ?, ?)",
                      (thread_id, hid, os.getpid(), now())).lastrowid


def menu_closed(db, menu_id):
    db.execute("DELETE FROM menus_open WHERE id=?", (menu_id,))


def open_menu(db, thread_id):
    """The latest open menu recorded for a conversation (its hand-off and menu-server pid), or None."""
    return db.execute("SELECT * FROM menus_open WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()


def clear_menus(db, thread_id):
    """Forget every menu recorded for a conversation: Codex's turn there has ended, so none can be open."""
    db.execute("DELETE FROM menus_open WHERE thread_id=?", (thread_id,))


def handoff(db, hid):
    return db.execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()


def latest_handoff_for_thread(db, thread_id):
    return db.execute(
        "SELECT * FROM handoffs WHERE thread_id=? AND status!='superseded' ORDER BY id DESC LIMIT 1",
        (thread_id,)).fetchone()


def latest_draft(db, hid):
    return db.execute(
        "SELECT * FROM drafts WHERE handoff_id=? AND status!='replaced' ORDER BY id DESC LIMIT 1",
        (hid,)).fetchone()
