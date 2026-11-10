"""Getting text into the ticket's Codex conversation: one thread per project and label, always loaded
before queueing."""
import contextlib
import fcntl
import hashlib
import pathlib
import subprocess
import time

from . import codexrpc, store

OPS_INSTRUCTIONS = (pathlib.Path(__file__).parent / "ops_instructions.md").read_text()


def persona_for(cfg, cwd):
    """A persona is keyed by project folder; a worktree of that project gets the project's persona."""
    personas = {str(pathlib.Path(k).resolve()): v for k, v in cfg["personas"].items()}
    for folder in (pathlib.Path(cwd).resolve(), store.main_checkout(cwd)):
        if folder is not None and str(folder.resolve()) in personas:
            return personas[str(folder.resolve())]
    return None


def instructions_for(cfg, cwd):
    persona_path = persona_for(cfg, cwd)
    persona = pathlib.Path(persona_path).read_text().strip() if persona_path else ""
    return (persona + "\n\n" + OPS_INSTRUCTIONS).strip()


def ensure_thread(db, cfg, label, cwd):
    """Return the thread id for `label` in the project holding `cwd`, creating it on first use. The thread is
    loaded on return. Two sessions handing off at once for the same project and label share one thread: the
    check and the creation run under a lock for that conversation, so the second finds the first's."""
    codexrpc.ensure_daemon(cfg)
    project = store.project_of(cwd)
    with conversation_lock(project, label):
        row = db.execute("SELECT thread_id FROM threads WHERE project=? AND label=?", (project, label)).fetchone()
        if row:
            thread_id = row["thread_id"]
        else:
            thread_id = codexrpc.start_thread(cfg, cwd, instructions_for(cfg, cwd), label)
            db.execute("INSERT INTO threads (project, label, thread_id, cwd, created) VALUES (?, ?, ?, ?, ?)",
                       (project, label, thread_id, cwd, store.now()))
            store.event(db, "thread:created", f"{project} {label} -> {thread_id}")
    codexrpc.ensure_loaded(cfg, thread_id)
    return thread_id


@contextlib.contextmanager
def conversation_lock(project, label):
    """An exclusive lock, across processes, on one project-and-label conversation."""
    locks = store.relay_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{project}\0{label}".encode()).hexdigest()[:32]
    with open(locks / f"thread-{key}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def queue(db, cfg, thread_id, text, client_id):
    """Queue `text` and confirm the thread is still loaded, so opening the window can never be what runs it."""
    item_id = codexrpc.queue_message(cfg, thread_id, text, client_id)
    codexrpc.ensure_loaded(cfg, thread_id)
    return item_id


HANDOVER_FILE = pathlib.Path(".scratch") / "codex-handover.md"


def pending_handover(db, thread_id, cwd):
    """The user's standing context from the main checkout's .scratch/codex-handover.md, if this thread has not
    yet been sent its current content. Fail-soft: a missing or unreadable file means no handover."""
    path = (store.main_checkout(cwd) or pathlib.Path(cwd)) / HANDOVER_FILE
    try:
        text = path.read_text().strip()
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    sha = hashlib.sha256(text.encode()).hexdigest()
    row = db.execute("SELECT sha256 FROM handovers_sent WHERE thread_id=?", (thread_id,)).fetchone()
    if row and row["sha256"] == sha:
        return None
    return {"path": str(path), "text": text, "sha256": sha, "mtime": mtime}


def mark_handover_sent(db, thread_id, sha):
    """Record the content a thread was sent; call only after the hand-off is queued, so a failure re-sends."""
    db.execute("INSERT OR REPLACE INTO handovers_sent (thread_id, sha256, sent) VALUES (?, ?, ?)",
               (thread_id, sha, store.now()))


def handover_block(handover):
    if not handover:
        return ""
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(handover["mtime"]))
    return (
        f"--- The user's standing context ({handover['path']}, updated {updated}) ---\n"
        "Read this before assessing. It is sent again whenever the file changes. Check its status claims "
        "against the repository before relying on them; where it conflicts with repository documentation, "
        "flag the conflict rather than override either.\n\n"
        f"{handover['text']}\n"
        "--- end of the user's standing context ---\n\n"
    )


def handoff_text(h, handover=None, last=None, sessions=""):
    """The hand-off as Codex receives it. `last` is the instruction this session last received, from
    store.last_delivered, so Codex knows what Claude was told. `sessions` is the list of the ticket's sessions
    as Codex is told it (claude_hooks.sessions_text)."""
    stamp = time.strftime("%H:%M", time.localtime(h["created"]))
    name = h["display_name"] if "display_name" in h.keys() else None
    sender = f" · from {name}" if name else ""
    where = ""
    if all(k in h.keys() for k in ("cwd", "branch", "head")) and h["cwd"]:
        where = f"Sent from {store.where(h)}\n"
    if last:
        where += f"Last instruction delivered: D{last[0]}, source {store.session_name(last[1])} H{last[1]['id']}\n"
    if sessions:
        where += f"{sessions}\n"
    return (
        f"[cc-codex-relay handoff H{h['id']} · ticket {h['label']}{sender} · Claude session {h['session_id'][:8]} · {stamp}]\n"
        "Reviewer instructions apply: assess this; if Claude needs an instruction, call propose_reply with either the exact prompt to send back "
        "(draft) or a copy of this report made by the relay (copy, optionally with your additions), never both; "
        "the sessions are isolated, so the delivered text must stand alone, "
        f"passing handoff \"H{h['id']}\". If Claude needs no instruction, don't draft an acknowledgement: "
        "say so and call no_reply_menu instead, recommending Finish. "
        "Anything for the owner (your assessment, the checks you ran) goes in the note or in chat, never in the "
        "draft. You may draft for a linked session that isn't held: the owner then gets Copy.\n"
        f"{where}\n"
        f"{handover_block(handover)}"
        "--- Claude's final output ---\n"
        f"{h['claude_message']}\n"
        "--- end of Claude's output ---"
    )


SESSION_ENDED_NOTICE = (
    "The originating Claude session has ended. This draft has not been sent. "
    "Review any outstanding concerns and tell the user what still needs attention."
)
