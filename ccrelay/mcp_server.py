"""Stdio MCP server spawned by the reviewer Codex daemon: stores Codex's drafts and shows the user the menu.

The menu is an MCP form elicitation. Only the user's answer to it can approve a draft; nothing Codex
passes as tool arguments can.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

from . import claude_hooks, codexrpc, store
from .notify import notify

DISCUSS, KEEP, FINISH = "Discuss", "Keep waiting", "Finish without sending"
COPY = "Copy to clipboard"
ABORTED = object()
# Closed hand-offs (DESIGN: Finish closes the handoff; a newer turn supersedes). Released, timed-out and
# ended hand-offs stay open to the tools, because they leave the draft Copy-only rather than gone.
CLOSED = ("superseded", "finished")

HANDOFF_PROP = {"type": "string", "description": "The handoff your draft answers, e.g. H5, from the handoff "
                                                 "header. Required."}
TOOLS = [
    {"name": "propose_reply",
     "description": "Save your drafted return prompt for Claude and show the user the review menu. "
                    "Call after every assessment and after each revision. Returns the user's choice.",
     "inputSchema": {"type": "object", "properties": {
         "draft": {"type": "string", "description": "The exact, complete prompt to send to Claude."},
         "handoff": HANDOFF_PROP,
         "recipient": {"type": "string", "description": "Optional: the session you suggest should receive "
                                                        "the draft. Shown to the user as text only."}},
         "required": ["draft", "handoff"]}},
    {"name": "show_menu",
     "description": "Show the user the review menu again for the draft on one handoff. Returns the user's choice.",
     "inputSchema": {"type": "object", "properties": {"handoff": HANDOFF_PROP}, "required": ["handoff"]}},
]


class Server:
    def __init__(self):
        self.out_lock = threading.Lock()
        self.pending = {}
        self.next_eid = 1

    def send(self, obj):
        with self.out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def elicit(self, message, choices, abort=None):
        """Show the form and wait for the user. If `abort()` turns true first, withdraw the form."""
        eid = f"relay-{self.next_eid}"
        self.next_eid += 1
        done = threading.Event()
        slot = {}
        self.pending[eid] = (done, slot)
        self.send({"jsonrpc": "2.0", "id": eid, "method": "elicitation/create", "params": {
            "mode": "form", "message": message,
            "requestedSchema": {"type": "object", "required": ["choice"], "properties": {
                "choice": {"type": "string", "title": "Choice", "enum": choices}}}}})
        while not done.wait(2):
            reason = abort() if abort else None
            if reason:
                self.pending.pop(eid, None)
                self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": eid, "reason": reason}})
                return ABORTED
        result = slot["msg"].get("result") or {}
        if result.get("action") != "accept":
            return None
        return (result.get("content") or {}).get("choice")

    def run(self):
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid, method = msg.get("id"), msg.get("method")
            if method is None and mid in self.pending:
                done, slot = self.pending.pop(mid)
                slot["msg"] = msg
                done.set()
            elif method == "initialize":
                self.send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cc-codex-relay", "version": "1"}}})
            elif method == "tools/list":
                hidden = (store.relay_home() / "test-hide-tools").exists()
                self.send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [] if hidden else TOOLS}})
            elif method == "tools/call":
                threading.Thread(target=self.call_tool, args=(mid, msg["params"]), daemon=True).start()
            elif method == "ping":
                self.send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})

    def call_tool(self, mid, params):
        try:
            meta = (params.get("_meta") or {}).get("x-codex-turn-metadata") or {}
            text = tool(self, params.get("name"), params.get("arguments") or {}, meta.get("session_id"))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:  # report every failure to Codex, never hang the turn
            result = {"content": [{"type": "text", "text": f"Relay error: {e}"}], "isError": True}
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})


def parse_handoff_ref(ref):
    """H5, h5, 5 or the integer 5 -> 5; anything else -> None."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref
    m = re.fullmatch(r"\s*[Hh]?(\d+)\s*", ref) if isinstance(ref, str) else None
    return int(m.group(1)) if m else None


def named_handoff(db, thread_id, ref):
    """The hand-off Codex named, or (None, reason). Never the newest one by default."""
    hid = parse_handoff_ref(ref)
    if hid is None:
        return None, ("Rejected: name the handoff you are answering, e.g. handoff \"H5\" "
                      "(it is in the handoff header). Nothing was saved or shown.")
    h = store.handoff(db, hid)
    if not h or h["thread_id"] != thread_id:
        why = "does not exist" if not h else "belongs to another conversation"
        return None, (f"Rejected: handoff H{hid} {why}. Use the H<n> from this conversation's handoff header. "
                      "Nothing was saved or shown.")
    if h["status"] in CLOSED:
        why = "a newer turn replaced it" if h["status"] == "superseded" else "it was finished without sending"
        return None, (f"Rejected: handoff H{hid} is closed ({h['status']}: {why}). "
                      "Answer a handoff that is still open. Nothing was saved or shown.")
    return h, None


def tool(server, name, args, thread_id):
    db = store.connect()
    cfg = store.load_config()
    if not thread_id:
        return "Relay error: this call carried no conversation id."
    if name not in ("propose_reply", "show_menu"):
        return f"Relay error: unknown tool {name}"
    h, rejection = named_handoff(db, thread_id, args.get("handoff"))
    if rejection:
        return rejection
    # The whole tool call keeps Codex's turn open, so a report arriving at any point in it waits behind it (Q7).
    menu_id = store.menu_opened(db, thread_id, h["id"])
    try:
        return menu_call(server, db, cfg, name, args, h)
    finally:
        store.menu_closed(db, menu_id)


def menu_call(server, db, cfg, name, args, h):
    if name == "propose_reply":
        draft = args.get("draft")
        if not isinstance(draft, str) or not draft.strip():
            return "Relay error: propose_reply needs a non-empty draft."
        recipient = args.get("recipient")
        recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None
        if h["status"] == "sent":
            return (f"Not saved: handoff H{h['id']} is already {h['status']}. "
                    "Wait for Claude's next output before drafting.")
        save_draft(db, cfg, h, draft, recipient)
    return menu(server, db, cfg, h["id"])


def save_draft(db, cfg, h, text, recipient=None):
    first = store.latest_draft(db, h["id"]) is None
    db.execute("UPDATE drafts SET status='replaced', updated=? WHERE handoff_id=? AND status IN ('ready','failed')",
               (store.now(), h["id"]))
    drafts_dir = store.relay_home() / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    t = store.now()
    did = db.execute("INSERT INTO drafts (handoff_id, text, path, status, suggested_recipient, created, updated)"
                     " VALUES (?, ?, '', 'ready', ?, ?, ?)", (h["id"], text, recipient, t, t)).lastrowid
    path = drafts_dir / f"{safe(h['label'])}-H{h['id']}-D{did}.md"
    path.write_text(text)
    db.execute("UPDATE drafts SET path=? WHERE id=?", (str(path), did))
    store.event(db, "draft:ready", f"D{did}", h["id"])
    store.set_handoff(db, h["id"], "review_ready", only_from=("assessing", "no_draft"))
    if first:
        notify(db, f"{store.about(db, h)}: review ready", "Open the Codex conversation to choose Send, Discuss, Keep waiting or Finish.",
               h["id"])


def safe(label):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:60]


def resolve_stale(db, h):
    """Bring the record in line with reality when this session's waiting hook has gone away, including a send
    to it that was interrupted: its transcript decides whether that draft arrived."""
    if h["status"] in store.WAITING and not claude_hooks.pid_alive(h["hook_pid"]):
        store.set_handoff(db, h["id"], "released", "Claude is no longer waiting (Esc, exit or hook stopped)",
                          only_from=store.WAITING)
    if not claude_hooks.pid_alive(h["hook_pid"]):
        for d in store.addressed_to(db, h["id"], "sending"):
            if claude_hooks.transcript_has_feedback(h["transcript_path"], d["text"]):
                store.delivered(db, d["id"], h["id"], "confirmed in Claude's transcript")
            else:
                store.set_draft(db, d["id"], "unknown", "hook stopped mid-send; not found in Claude's transcript",
                                only_from=("sending",))
    return store.handoff(db, h["id"])


def resolve_draft(db, d):
    """Resolve an interrupted send of draft `d` against the session it was addressed to; the draft as it now stands."""
    if d["status"] == "sending":
        resolve_stale(db, store.handoff(db, d["recipient_handoff_id"] or d["handoff_id"]))
    return db.execute("SELECT * FROM drafts WHERE id=?", (d["id"],)).fetchone()


# Hand-off states the user's Finish may close. A hand-off that was sent something since is left as it is.
FINISHABLE = store.WAITING + ("released", "timed_out", "session_ended", "failed")


def menu(server, db, cfg, hid, gone=None):
    """Show the menu for the draft on hand-off `hid` and act on the user's choice. `gone` is a recipient that
    stopped waiting when the user chose Send to it: the menu returns saying so, with Copy for it."""
    h = resolve_stale(db, store.handoff(db, hid))
    d = store.latest_draft(db, hid)
    if not d:
        return f"No draft yet for H{hid}. Draft one and call propose_reply."
    d = resolve_draft(db, d)
    waiting = h["status"] in store.WAITING
    went_to = store.handoff(db, d["recipient_handoff_id"]) if (
        d["status"] == "sent" and d["recipient_handoff_id"] not in (None, hid)) else None
    if d["status"] == "sent" and not (went_to and waiting):
        if went_to:
            return (f"Already sent: D{d['id']} went to {store.session_name(went_to)} (H{went_to['id']}). "
                    f"H{hid} is no longer waiting ({h['reason'] or h['status']}), so nothing more can be sent for it.")
        return f"Already sent to Claude (H{hid}, D{d['id']}). Wait for Claude's next output; it will arrive here."
    elsewhere = went_to is not None  # the source is still held after its draft went to another session
    sendable = waiting and d["status"] in ("ready", "failed")
    held = held_sessions(db, h) if sendable else []
    idle = sessions_not_held(db, h, [r["session_id"] for r in held + ([gone] if gone else [])]) if sendable else []
    names = unique_names([h, *held, *idle, *(r for r in (gone, went_to) if r)])

    state_line = {
        "released": "Claude is no longer waiting, so Send is not available. You can copy the draft.",
        "timed_out": "Claude's wait limit passed: timed out, not sent. You can copy the draft.",
        "session_ended": "The originating Claude session has ended. This draft has not been sent.",
    }.get(h["status"], "")
    if d["status"] == "unknown":
        state_line = ("This draft might have reached Claude (the send was interrupted). "
                      "Check Claude before resending, otherwise it could receive the instruction twice.")
    elif d["status"] == "failed":
        state_line = f"The last send failed: {d['reason']}."
    elif d["status"] == "closed":
        state_line = d["reason"][:1].upper() + d["reason"][1:] + "."
    elif elsewhere:
        state_line = (f"D{d['id']} was sent to {names[went_to['id']]} (H{went_to['id']}). {names[hid]} is still "
                      f"waiting: draft something for it with propose_reply H{hid}, or finish it.")

    copy = f"Copy for {names[gone['id']]}" if gone else COPY
    finish = f"{FINISH} — release {names[hid]}"
    sends = {f"Send to {names[r['id']]}": r for r in held}
    copies = {f"Copy for {names[r['id']]}": r for r in idle}
    destinations = [destination_line(r, names[r["id"]], r["id"] == hid) for r in held] + \
                   [f"- {names[r['id']]}: not waiting ({not_held_reason(r)}); Copy only" for r in idle]
    if sendable:
        choices = ([copy] if gone else []) + [*sends, *copies, DISCUSS, KEEP, finish]
    elif elsewhere:
        choices = [DISCUSS, KEEP, finish]
    else:
        choices = [copy, DISCUSS, finish]

    poll = {}

    def source_stopped():
        """Withdraw the menu once the source stops waiting, if it offers anything that needs it held."""
        if not (sends or elsewhere):
            return None
        conn = poll.setdefault("db", store.connect())
        cur = resolve_stale(conn, store.handoff(conn, hid))
        return None if cur["status"] in store.WAITING else cur["status"]

    waiting_reports = [f"H{q['id']} from {store.session_name(q)}" for q in reports_queued(db, cfg, h)]
    if waiting_reports:
        state_line = "\n".join(p for p in (state_line, "Reports waiting for assessment: " + ", ".join(waiting_reports)) if p)
    message = menu_message(cfg, h, d, state_line, destinations)
    if gone:  # why nothing was sent comes first
        message = not_sent_line(gone, names[gone["id"]]) + "\n" + message
    answer = server.elicit(message, choices, abort=source_stopped)
    if answer is ABORTED:
        h = store.handoff(db, hid)
        store.event(db, "menu:withdrawn", f"D{d['id']} {h['status']}", hid)
        if h["status"] == "session_ended":
            return ("The menu was withdrawn: the originating Claude session has ended and the draft was not sent. "
                    "The relay's notice follows as the next message; end your turn now.")
        return (f"The menu was withdrawn: Claude is no longer waiting ({h['reason'] or h['status']}). "
                "The draft was not sent. Tell the user in one line, then end your turn; call show_menu if they want to copy it.")
    store.event(db, "menu:answer", f"D{d['id']} {answer!r}", hid)
    if answer in sends:
        r = resolve_stale(db, store.handoff(db, sends[answer]["id"]))
        result = send(db, cfg, h, d, r, names) if r["status"] in store.WAITING else None
        if result is None:  # the recipient stopped waiting: nothing was sent, and the user decides what next
            return menu(server, db, cfg, hid, gone=store.handoff(db, r["id"]))
        return result
    if answer == KEEP:
        return ("The user is keeping Claude waiting. End your turn now with no further output; "
                "call show_menu if they later ask for the menu.")
    if answer == finish:
        if not store.set_handoff(db, hid, "finished", "the user finished without sending", only_from=FINISHABLE):
            return (f"Nothing to release: {names[hid]} (H{hid}) is {store.handoff(db, hid)['status']} now. "
                    "Nothing was sent.")
        return "Claude was released without a reply. Nothing was sent."
    if answer == copy or answer in copies:
        return copy_draft(db, d)
    queued = reports_queued(db, cfg, h)
    first = ""
    if queued:
        first = (" " + "; ".join(f"Report H{q['id']} ({store.session_name(q)})" for q in queued)
                 + (" is queued in this conversation and runs" if len(queued) == 1 else
                    " are queued in this conversation and run") + " first, as soon as "
                 "you end your turn. Assess it then; the user's discussion of this draft continues after it.")
    return ("The user wants to discuss. End your turn now with no further output and wait for their message."
            f"{first} When you have a revised draft, call propose_reply again.")


def reports_queued(db, cfg, h):
    """Other reports on this conversation still in Codex's queue, not yet assessed: Codex runs each once the
    current turn ends. A report Codex is assessing now isn't one. If Codex can't be asked, every report not
    yet drafted for is counted."""
    rows = db.execute("SELECT * FROM handoffs h WHERE thread_id=? AND id!=? AND status IN ('queued', 'assessing')"
                      " AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.handoff_id=h.id) ORDER BY id",
                      (h["thread_id"], h["id"])).fetchall()
    try:
        in_queue = codexrpc.queued_ids(cfg, h["thread_id"])
    except (OSError, codexrpc.CodexError):
        return rows
    return [q for q in rows if q["queue_item_id"] in in_queue]


def held_sessions(db, h):
    """Hand-offs whose sessions are held in their Stop hook on the source's conversation (the same project and
    ticket), the source first, then in the order they reported."""
    rows = db.execute(f"SELECT * FROM handoffs WHERE thread_id=? AND status IN ({','.join('?' * len(store.WAITING))})"
                      " ORDER BY id", (h["thread_id"], *store.WAITING)).fetchall()
    held = [r for r in (resolve_stale(db, r) for r in rows) if r["status"] in store.WAITING]
    # A hook started before recipients existed collects drafts by the report they answer, not by recipient: its
    # session can be sent only its own drafts, and its drafts only to it.
    if not h["recipient_aware"]:
        held = [r for r in held if r["id"] == h["id"]]
    held = [r for r in held if r["id"] == h["id"] or r["recipient_aware"]]
    return sorted(held, key=lambda r: r["id"] != h["id"])


def sessions_not_held(db, h, exclude):
    """The latest hand-off of each other session still linked to this ticket that is busy (it received an
    instruction), released, finished, timed out or failed: it can't be sent to, so it is offered Copy."""
    return db.execute("SELECT * FROM handoffs WHERE id IN (SELECT MAX(id) FROM handoffs WHERE thread_id=? "
                      "GROUP BY session_id) AND status IN ('sent', 'released', 'finished', 'timed_out', 'failed') "
                      "AND session_id IN (SELECT session_id FROM links WHERE label=?) "
                      f"AND session_id NOT IN ({','.join('?' * len(exclude))}) ORDER BY id",
                      (h["thread_id"], h["label"], *exclude)).fetchall()


def destination_line(r, name, source):
    """A held session as the menu shows it, with where it was and when that was captured."""
    captured = time.strftime("%H:%M", time.localtime(r["created"]))
    return f"- {name}{' (source)' if source else ''}, H{r['id']}: {store.where(r)} · captured {captured}"


def unique_names(hs):
    """Names by hand-off id; two sessions with the same name are told apart by short session ID."""
    sessions = {}
    for r in hs:
        sessions.setdefault(store.session_name(r), set()).add(r["session_id"])
    names = {}
    for r in hs:
        n = store.session_name(r)
        names[r["id"]] = n if len(sessions[n]) == 1 else f"{n} ({r['session_id'][:6]})"
    return names


def menu_message(cfg, h, d, state_line, destinations=()):
    lines = d["text"].splitlines()
    if len(lines) <= cfg["menu_inline_lines"] and len(d["text"]) <= cfg["menu_inline_chars"]:
        body = d["text"]
    else:
        body = "\n".join(lines[:15]) + f"\n… ({len(lines)} lines, {len(d['text'])} characters)"
    parts = [f"Answering report H{h['id']} · ticket {h['label']} · draft D{d['id']} · Claude session {h['session_id'][:8]}"]
    if d["suggested_recipient"]:
        parts.append(f"Reviewer suggests sending to: {d['suggested_recipient']} (a suggestion only)")
    if state_line:
        parts.append(state_line)
    if destinations:
        parts += ["", "Sessions on this ticket:", *destinations]
    parts += ["", "Draft (sent exactly as stored):", body, "", f"Exact file: {d['path']}"]
    return "\n".join(parts)


def send(db, cfg, h, d, r, names):
    """Deliver draft `d` (answering source hand-off `h`) to the session holding hand-off `r`; `names` are the
    menu's session names by hand-off id. None means nothing was sent because the recipient stopped waiting."""
    if not store.approve(db, d["id"], r["id"]):
        other = next(iter(store.addressed_to(db, r["id"], "approved") + store.addressed_to(db, r["id"], "sending")), None)
        if other and draft_status(db, d) in ("ready", "failed"):
            return (f"Not sent: {names[r['id']]} is already receiving D{other['id']}. The draft is kept; "
                    "call show_menu to send it once that has arrived.")
        return f"Not sent: draft D{d['id']} is {draft_status(db, d)}."
    deadline = time.time() + cfg["send_pickup_secs"]
    while time.time() < deadline:
        cur = draft_status(db, d)
        if cur == "sent":
            return sent_notice(h, d, r, names)
        if cur != "approved":
            time.sleep(0.3)
            continue
        if not claude_hooks.pid_alive(r["hook_pid"]):
            break
        time.sleep(0.3)
    if store.set_draft(db, d["id"], "failed", "Claude's hook did not pick up the draft", only_from=("approved",)):
        if resolve_stale(db, store.handoff(db, r["id"]))["status"] not in store.WAITING:
            return None
        return (f"Not sent: {names[r['id']]} is still waiting, but its hook did not collect D{d['id']} within "
                f"{cfg['send_pickup_secs']} s. The draft is kept; call show_menu to try again.")
    resolve_stale(db, store.handoff(db, r["id"]))
    status = draft_status(db, d)
    if status == "sent":
        return sent_notice(h, d, r, names)
    return (f"Send state is '{status}': it might have reached Claude. Tell the user to check Claude before any resend.")


def not_held_reason(r):
    return "busy: it received an instruction and is working" if r["status"] == "sent" else r["reason"] or r["status"]


def not_sent_line(r, name):
    """Q10: why a Send didn't happen, for a recipient that is no longer held."""
    return (f"Not sent: {name} stopped waiting at {time.strftime('%H:%M', time.localtime(r['updated']))} "
            f"({not_held_reason(r)}).")


def draft_status(db, d):
    return db.execute("SELECT status FROM drafts WHERE id=?", (d["id"],)).fetchone()["status"]


def sent_notice(h, d, r, names):
    """What Codex is told after a delivery. A send to another session says the source is still waiting."""
    if r["id"] == h["id"]:
        return "Sent to Claude. Claude is working; its next output will arrive here automatically."
    source = names[h["id"]]
    return (f"Sent D{d['id']} to {names[r['id']]} (H{r['id']}). {source[:1].upper() + source[1:]} H{h['id']} is still "
            f"waiting; use show_menu H{h['id']} to send it something or finish it.")


def copy_draft(db, d):
    clip = shutil.which("clip.exe")
    if clip:
        subprocess.run([clip], input=d["text"].encode("utf-16-le"), check=False)
        store.set_draft(db, d["id"], "copied", "copied to clipboard", only_from=("ready", "failed", "unknown"))
        return "Copied the draft to the user's clipboard. Nothing was sent to Claude."
    return f"No clipboard here. The exact draft is in {d['path']}. Nothing was sent to Claude."


def main():
    Server().run()
