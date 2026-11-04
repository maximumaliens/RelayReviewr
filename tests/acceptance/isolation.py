"""Guards that keep acceptance runs off the user's installed relay.

A run snapshots the installed relay's store before it starts and compares it row by row at the end. A new,
removed or replaced row carrying one of the run's test session IDs, test labels or a path under the
acceptance roots in its identity columns, or belonging to a hand-off or Codex thread that does, makes the run
invalid. Message text never counts. Every other changed row is listed as not attributed to the run, never
dropped. The store is opened read-only and never through ccrelay.store, whose connect() would create
the schema and migrate it.
"""
import pathlib
import sqlite3

HOME = pathlib.Path.home()
INSTALLED_STATE = HOME / ".local/state/cc-codex-relay"
INSTALLED_STORE = INSTALLED_STATE / "relay.db"
# The installed relay's state, its reviewer Codex home, the user's own Codex home and Claude configuration.
INSTALLED = (INSTALLED_STATE, HOME / ".codex-reviewer", HOME / ".codex", HOME / ".claude")


def snapshot(path=INSTALLED_STORE):
    """{table: {rowid: {column: value}}} for the store at `path`, or None when it doesn't exist. Rows still
    in the write-ahead log are included."""
    path = pathlib.Path(path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in tables:
            cur = db.execute(f'SELECT rowid AS "__rowid", * FROM "{t}" ORDER BY rowid')
            columns = [d[0] for d in cur.description][1:]
            out[t] = {r[0]: dict(zip(columns, r[1:])) for r in cur}
        return out
    finally:
        db.close()


def changes(before, after):
    """Every new, removed or replaced row between two snapshots, with its contents: a list of
    {table, rowid, change, before, after}. A store that appeared or vanished is one more entry, with no row."""
    out = []
    if (before is None) != (after is None):
        out.append({"table": None, "rowid": None, "before": None, "after": None,
                    "change": "the store was created" if after is not None else "the store was removed"})
    before, after = before or {}, after or {}
    for t in sorted(set(before) | set(after)):
        b, a = before.get(t, {}), after.get(t, {})
        for rowid in sorted(set(b) | set(a)):
            old, new = b.get(rowid), a.get(rowid)
            if old != new:
                out.append({"table": t, "rowid": rowid, "before": old, "after": new,
                            "change": "new" if old is None else "removed" if new is None else "replaced"})
    return out


# Columns that name a session, a ticket or a place; message text (Claude's reply, a draft, a notice) never counts.
PATH_COLUMNS = ("cwd", "project", "transcript_path", "path")
# Event kinds whose detail is a session ID or label in a fixed shape (ccrelay.cli, claude_hooks, intake).
SID_EVENTS = ("unlink", "hook:skipped_background")


def under(value, roots):
    """Whether `value` is one of `roots` or a path inside one."""
    return isinstance(value, str) and any(value == r or value.startswith(r + "/") for r in roots)


def carries_mark(table, row, marks, roots):
    """Whether a row names one of the run's sessions, labels or acceptance paths in its own identity columns."""
    if row.get("session_id") in marks["session_ids"] or row.get("label") in marks["labels"]:
        return True
    if any(under(row.get(c), roots) for c in PATH_COLUMNS):
        return True
    if table != "events" or not isinstance(row.get("detail"), str):
        return False
    kind, detail = row["kind"], row["detail"]
    if kind in ("link", "link:once", "link:no_conversation"):  # "<session> -> <label>"
        sid, _, label = detail.partition(" -> ")
        return sid in marks["session_ids"] or label in marks["labels"]
    if kind == "thread:created":  # "<project> <label> -> <thread>"
        head = detail.rpartition(" -> ")[0]
        return any(head.startswith(r + "/") or head.startswith(r + " ") for r in roots) or \
            any(head.endswith(" " + label) for label in marks["labels"])
    return (kind in SID_EVENTS and detail in marks["session_ids"]) or \
        (kind == "handoff:queued" and detail in marks["labels"])


def attribute(before, after, marks):
    """(rows this run wrote, every other changed row) between two snapshots. A row is this run's if it, before or
    after the change, carries one of `marks` ("session_ids", "labels", acceptance "roots"), or belongs to a hand-off
    or Codex thread that does in either snapshot: drafts, events and menus are found through them."""
    roots = {s.rstrip("/") for root in marks["roots"] for s in (str(root), str(pathlib.Path(root).resolve()))}
    rows = [(t, r) for snap in (before, after) if snap for t, table in snap.items() for r in table.values()]
    marked = [(t, r) for t, r in rows if carries_mark(t, r, marks, roots)]
    handoffs = {r["id"] for t, r in marked if t == "handoffs"}
    threads = {r["thread_id"] for t, r in marked if t in ("threads", "handoffs") and r.get("thread_id")}

    def mine(change):
        return any(carries_mark(change["table"], r, marks, roots) or r.get("thread_id") in threads
                   or r.get("handoff_id") in handoffs or r.get("recipient_handoff_id") in handoffs
                   for r in (change["before"], change["after"]) if r)
    changed = changes(before, after)
    return [c for c in changed if mine(c)], [c for c in changed if not mine(c)]


def describe(change, limit=200):
    """One summary line for a changed row; string values longer than `limit` are cut (t<NN>.json keeps them)."""
    if change["table"] is None:
        return change["change"]

    def row(r):
        return repr({k: v[:limit] + "…" if isinstance(v, str) and len(v) > limit else v for k, v in r.items()})
    body = " -> ".join(row(r) for r in (change["before"], change["after"]) if r is not None)
    return f"{change['table']} row {change['rowid']}, {change['change']}: {body}"


def guard(mine, other, store=INSTALLED_STORE):
    """(the summary's opening section, run.py's exit code). Only rows this run wrote make it invalid."""
    if mine:
        text = (f"**INVALID run: this run wrote to the installed relay's store** ({store}):\n"
                + "".join(f"- {describe(c)}\n" for c in mine))
    elif other:
        text = f"Isolation check: the installed relay's store ({store}) holds no row this run wrote.\n"
    else:
        text = f"Isolation check: the installed relay's store ({store}) is unchanged.\n"
    if other:
        text += ("\nNot attributed to this run (rows carrying none of its session IDs, labels or acceptance paths, "
                 "such as the user's own sessions; check none is a leak):\n"
                 + "".join(f"- {describe(c)}\n" for c in other))
    return text, 1 if mine else 0


def refuse_installed(path):
    """Stop if `path` is, or is inside, one of the user's installed locations."""
    p = pathlib.Path(path).expanduser().resolve()
    for root in INSTALLED:
        r = root.resolve()
        if p == r or r in p.parents:
            raise SystemExit(f"acceptance: refusing to use {p}: it belongs to the installed setup ({r})")
