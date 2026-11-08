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

from . import claude_hooks, codexrpc, draft_copy, store
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
                    "Call when Claude needs an instruction, and after each revision. If Claude needs no instruction "
                    "(its report only restates a decision request, acknowledges, or waits for the owner), do not "
                    "draft an acknowledgement: call no_reply_menu instead. Returns the user's choice. "
                    "Discuss keeps Claude held: if the owner wants to answer Claude directly, recommend Finish "
                    "first and call show_menu so they can. "
                    "Give EITHER `draft` (free text, sent exactly as you write it) OR `copy` (the relay copies "
                    "the stored report named by `handoff` itself, so nothing is retyped), optionally followed by "
                    "your `additions`; never `draft` together with `copy` or `additions`. "
                    "The sessions are isolated: the recipient has seen none of your discussion and none of the "
                    "other session's reports, so the delivered text must stand alone, naming the exact files and "
                    "commands, and must not depend on H or D identifiers. "
                    "To pass a builder's closeout to a verifier, use copy \"all\" and put any material "
                    "correction or concern under \"Reviewer notes\" in `additions`; with nothing to add, copy "
                    "alone. To send a verifier's findings to a builder, copy only the accepted builder-directed "
                    "instructions (leave out commentary and rejected or superseded instructions) and put your "
                    "amendments in `additions`, with no conflicting requirements.",
     "inputSchema": {"type": "object", "properties": {
         "draft": {"type": "string", "description": "Free text: the exact, complete prompt to send to Claude. "
                                                    "Not together with copy or additions."},
         "copy": {"anyOf": [{"type": "string", "enum": ["all"]},
                            {"type": "array", "items": {"type": "object", "properties": {
                                "from": {"type": "string", "description": "Text of the first line to copy "
                                                                          "(whitespace at both ends ignored)."},
                                "to": {"type": "string", "description": "Text of the last line to copy, matched "
                                                                        "at or after the first."},
                                "from_line": {"type": "integer", "description": "Line number, only to resolve a "
                                                                                "repeated `from`, as the relay lists."},
                                "to_line": {"type": "integer", "description": "Line number, only to resolve a "
                                                                              "repeated `to`, as the relay lists."}},
                                "required": ["from", "to"]}}],
                  "description": "Instead of draft: \"all\" copies the whole stored report; or a list of ascending, "
                                 "non-overlapping {from, to} line selections. The relay copies the text exactly."},
         "additions": {"type": "string", "description": "Only with copy: your own text placed after the copied "
                                                        "text, e.g. \"Reviewer notes\" or \"Amendments\"."},
         "handoff": HANDOFF_PROP,
         "recipient": {"type": "string", "description": "Optional: the session you suggest should receive "
                                                        "the draft. Shown to the user as text only."}},
         "required": ["handoff"]}},
    {"name": "show_menu",
     "description": "Show the user the review menu again for the draft on one handoff, or the no-reply menu "
                    "again if you assessed that Claude needs no reply. Returns the user's choice. Discuss keeps "
                    "Claude held: if the owner wants to answer Claude directly, recommend Finish first and call "
                    "this so they can.",
     "inputSchema": {"type": "object", "properties": {"handoff": HANDOFF_PROP}, "required": ["handoff"]}},
    {"name": "no_reply_menu",
     "description": "Use instead of propose_reply when Claude needs no instruction: its report only restates a "
                    "decision request, acknowledges, or waits for the owner. Do not draft an acknowledgement; "
                    "say so to the owner, call this for the handoff, and recommend Finish. Any reply Claude gives "
                    "comes back as another handoff. Shows a menu with Discuss, Keep waiting and Finish without "
                    "sending, never Send or Copy, and returns the user's choice. Only for a handoff that has no "
                    "draft; nothing is sent or finished unless the user chooses it. Discuss keeps Claude held: if the owner "
                    "wants to answer Claude directly, recommend Finish first. Use show_menu to reopen it; "
                    "a draft you save later brings back the normal menu.",
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
    if name not in ("propose_reply", "show_menu", "no_reply_menu"):
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
    if name == "no_reply_menu":
        rejection = record_no_reply(db, cfg, h)
        if rejection:
            return rejection
    if name == "propose_reply":
        draft, copy, additions = (args.get(k) for k in ("draft", "copy", "additions"))  # null = left out
        parts = None
        if draft is not None and (copy is not None or additions is not None):
            return ("Rejected: give either draft (free text) or copy with optional additions, not both. "
                    "Nothing was saved or shown.")
        if copy is None and additions is not None:
            return "Rejected: additions only go with copy. Nothing was saved or shown."
        if copy is not None:
            try:
                draft, parts = draft_copy.assemble(h["claude_message"], copy, additions)
            except draft_copy.CopyError as e:
                return str(e)
        if not isinstance(draft, str) or not draft.strip():
            return "Relay error: propose_reply needs a non-empty draft."
        recipient = args.get("recipient")
        recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None
        if h["status"] == "sent":
            return (f"Not saved: handoff H{h['id']} is already {h['status']}. "
                    "Wait for Claude's next output before drafting.")
        save_draft(db, cfg, h, draft, recipient, parts)
    return menu(server, db, cfg, h["id"])


def record_no_reply(db, cfg, h):
    """Record Codex's assessment that hand-off `h` needs no reply; a reason if it can't be, with nothing written.
    A hand-off already assessed this way just reopens its menu."""
    if h["status"] == "sent":
        return (f"Not saved: handoff H{h['id']} is already sent. Wait for Claude's next output before assessing it. "
                "Nothing was changed.")
    draft = store.latest_draft(db, h["id"])
    if draft:
        return (f"Rejected: handoff H{h['id']} already has a draft (D{draft['id']}), so it needs a reply from the "
                f"menu: use show_menu H{h['id']}. The draft and the handoff are unchanged.")
    if store.no_reply(db, h["id"]):
        return None
    h = resolve_stale(db, h)
    if h["status"] not in store.WAITING or not store.mark_no_reply(db, h["id"]):
        h = store.handoff(db, h["id"])
        if store.no_reply(db, h["id"]):
            return None  # another call assessed it first
        return (f"Not recorded: H{h['id']} is {h['status']} ({h['reason'] or 'no reason recorded'}): Claude is no "
                "longer waiting, or the report is not being assessed yet. Nothing was changed.")
    try:  # a notice never changes the recorded assessment
        notify(db, f"{store.about(db, h)}: review ready — no reply needed",
               "Open the Codex conversation to choose Discuss, Keep waiting or Finish.", h["id"])
    except Exception as e:  # any failure of a notice
        store.event(db, "notify_failed", str(e), h["id"])
    return None


def save_draft(db, cfg, h, text, recipient=None, parts=None):
    first = store.latest_draft(db, h["id"]) is None
    db.execute("UPDATE drafts SET status='replaced', updated=? WHERE handoff_id=? AND status IN ('ready','failed')",
               (store.now(), h["id"]))
    drafts_dir = store.relay_home() / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    t = store.now()
    did = db.execute("INSERT INTO drafts (handoff_id, text, path, status, suggested_recipient, created, updated)"
                     " VALUES (?, ?, '', 'ready', ?, ?, ?)", (h["id"], text, recipient, t, t)).lastrowid
    path = drafts_dir / f"{safe(h['label'])}-H{h['id']}-D{did}.md"
    path.write_bytes(text.encode("utf-8"))  # raw bytes: no locale encoding, no newline translation
    db.execute("UPDATE drafts SET path=? WHERE id=?", (str(path), did))
    for seq, part in enumerate(parts or []):
        db.execute("INSERT INTO draft_parts (draft_id, seq, kind, source_handoff_id, start_char, end_char, from_line,"
                   " to_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (did, seq, part["kind"], h["id"] if part["kind"] == "copied" else None, part["start"], part["end"],
                    part.get("from_line"), part.get("to_line")))
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
        if store.no_reply(db, hid):
            return no_reply_screen(server, db, cfg, h)
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
    parts = draft_parts(db, d)
    message = menu_message(cfg, h, d, state_line, destinations, parts, provenance_line(db, parts) if parts else None)
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
        return KEEP_RESULT
    if answer == finish:
        return finish_result(db, names[hid], hid)
    if answer == copy or answer in copies:
        return copy_draft(db, d)
    return discuss_result(db, cfg, h, "this draft", "When you have a revised draft, call propose_reply again.")


KEEP_RESULT = ("The user is keeping Claude waiting. End your turn now with no further output; "
               "call show_menu if they later ask for the menu.")


def finish_result(db, name, hid):
    if not store.set_handoff(db, hid, "finished", "the user finished without sending", only_from=FINISHABLE):
        return (f"Nothing to release: {name} (H{hid}) is {store.handoff(db, hid)['status']} now. "
                "Nothing was sent.")
    return "Claude was released without a reply. Nothing was sent."


def discuss_result(db, cfg, h, subject, next_step):
    """What Codex is told after Discuss: end the turn, a queued report runs first, and the source stays held (so
    the owner must Finish before answering Claude directly)."""
    queued = reports_queued(db, cfg, h)
    first = ""
    if queued:
        first = (" " + "; ".join(f"Report H{q['id']} ({store.session_name(q)})" for q in queued)
                 + (" is queued in this conversation and runs" if len(queued) == 1 else
                    " are queued in this conversation and run") + " first, as soon as "
                 f"you end your turn. Assess it then; the user's discussion of {subject} continues after it.")
    held = ""
    if store.handoff(db, h["id"])["status"] in store.WAITING:
        name = store.session_name(h)
        held = (f" {name[:1].upper() + name[1:]} (H{h['id']}) is still held: "
                "anything the user types in Claude queues until Send, Finish or Esc. If they want to answer Claude "
                "directly, tell them to choose Finish first, and call show_menu so they can.")
    return ("The user wants to discuss. End your turn now with no further output and wait for their message."
            f"{held}{first} {next_step}")


def no_reply_screen(server, db, cfg, h):
    """The menu for a hand-off Codex assessed as needing no reply: Discuss, Keep waiting, Finish; never a draft's
    Send or Copy. Once the source stopped waiting, Keep waiting means nothing and isn't offered."""
    hid = h["id"]
    waiting = h["status"] in store.WAITING
    names = unique_names([h, *held_sessions(db, h)]) if waiting else unique_names([h])
    finish = f"{FINISH} — release {names[hid]}"
    choices = [DISCUSS, KEEP, finish] if waiting else [DISCUSS, finish]
    state_line = "" if waiting else (f"Claude is no longer waiting ({h['reason'] or h['status']}). "
                                     "Nothing was sent.")
    waiting_reports = [f"H{q['id']} from {store.session_name(q)}" for q in reports_queued(db, cfg, h)]
    if waiting_reports:
        state_line = "\n".join(p for p in (state_line, "Reports waiting for assessment: " + ", ".join(waiting_reports)) if p)
    message = "\n".join(p for p in (
        f"Answering report H{hid} · ticket {h['label']} · Claude session {h['session_id'][:8]}",
        "Codex assessed that Claude needs no reply. There is no draft, and nothing will be sent.",
        state_line,
        f"Finish releases {names[hid]} without a reply." if waiting else "") if p)

    poll = {}

    def source_stopped():
        if not waiting:
            return None
        conn = poll.setdefault("db", store.connect())
        cur = resolve_stale(conn, store.handoff(conn, hid))
        return None if cur["status"] in store.WAITING else cur["status"]

    answer = server.elicit(message, choices, abort=source_stopped)
    if answer is ABORTED:
        h = store.handoff(db, hid)
        store.event(db, "menu:withdrawn", f"no-reply {h['status']}", hid)
        return (f"The menu was withdrawn: Claude is no longer waiting ({h['reason'] or h['status']}). Nothing was "
                "sent. Tell the user in one line, then end your turn.")
    store.event(db, "menu:answer", f"no-reply {answer!r}", hid)
    if answer == KEEP and waiting:
        return KEEP_RESULT
    if answer == finish:
        return finish_result(db, names[hid], hid)
    return discuss_result(db, cfg, h, "this report", "If Claude still needs no reply, call show_menu when the "
                          "user wants the menu; if it needs an instruction, call propose_reply.")


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


def draft_parts(db, d):
    """The parts a copied draft was assembled from, in order; none for a free-text draft."""
    return db.execute("SELECT * FROM draft_parts WHERE draft_id=? ORDER BY seq", (d["id"],)).fetchall()


def provenance_line(db, parts):
    """Metadata for the menu, never part of the delivered text: which lines were copied from which report and
    session, and how large the reviewer's additions are."""
    copied = [p for p in parts if p["kind"] == "copied"]
    source = store.handoff(db, copied[0]["source_handoff_id"])
    ranges = [f"{p['from_line']}-{p['to_line']}" if p["to_line"] != p["from_line"] else str(p["from_line"])
              for p in copied]
    noun = "line" if len(copied) == 1 and copied[0]["from_line"] == copied[0]["to_line"] else "lines"
    spans = ranges[0] if len(ranges) == 1 else ", ".join(ranges[:-1]) + " and " + ranges[-1]
    added = sum(p["end_char"] - p["start_char"] for p in parts if p["kind"] == "additions")
    return (f"Copied from report H{source['id']} ({store.session_name(source)}, Claude session "
            f"{source['session_id'][:8]}): {noun} {spans}; reviewer's additions: {added} characters.")


def preview(cfg, d, parts):
    """A long copied draft as a marked preview: the start of the copied text, then the reviewer's additions up
    to a cap. Everything shortened says so."""
    text = d["text"]
    adds = [p for p in parts if p["kind"] == "additions"]
    copied = text[:max(p["end_char"] for p in parts if p["kind"] == "copied")]
    lines = copied.splitlines()
    shown = "\n".join(lines[:15])
    cap = cfg["menu_inline_chars"]
    out = [shown[:cap]]
    if len(shown) > cap:
        out.append(f"… (copied part shortened: showing the first {cap} of {len(copied)} characters)")
    elif len(lines) > 15:
        out.append(f"… (copied part shortened: showing the first 15 of {len(lines)} lines, {len(copied)} characters)")
    if adds:
        extra = text[adds[0]["start_char"]:adds[0]["end_char"]]
        cap = cfg["menu_additions_chars"]
        out += ["", "Reviewer's additions:", extra[:cap]]
        if len(extra) > cap:
            out.append(f"… (additions shortened: showing the first {cap} of {len(extra)} characters)")
    return "\n".join(out)


def menu_message(cfg, h, d, state_line, destinations=(), parts=(), provenance=None):
    lines = d["text"].splitlines()
    shortened = not (len(lines) <= cfg["menu_inline_lines"] and len(d["text"]) <= cfg["menu_inline_chars"])
    label = "Draft (sent exactly as stored):"
    if not shortened:
        body = d["text"]
    elif parts:
        body = preview(cfg, d, parts)
        label = "Draft (shortened preview; the exact draft is sent as stored in the file below):"
    else:
        body = "\n".join(lines[:15]) + f"\n… ({len(lines)} lines, {len(d['text'])} characters)"
    parts_line = [provenance] if provenance else []
    head = [f"Answering report H{h['id']} · ticket {h['label']} · draft D{d['id']} · Claude session {h['session_id'][:8]}"]
    if d["suggested_recipient"]:
        head.append(f"Reviewer suggests sending to: {d['suggested_recipient']} (a suggestion only)")
    if state_line:
        head.append(state_line)
    if destinations:
        head += ["", "Sessions on this ticket:", *destinations]
    return "\n".join(head + parts_line + ["", label, body, "", f"Exact file: {d['path']}"])


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
