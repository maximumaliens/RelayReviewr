"""relay — command-line entry point."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from . import claude_hooks, codexrpc, intake, mcp_server, store

USAGE = """usage: relay <command>
  link [--as ROLE] [LABEL]         link this Claude session to a ticket (run from Claude via /relay-link)
  unlink                           stop forwarding this Claude session
  review-once [--as ROLE] [LABEL]  forward the previous turn once (run from Claude via /relay-review)
  review LABEL        open the ticket's Codex conversation (this project's, else the only match)
  status              show recent handoffs
  install-claude      add the relay hooks and commands to ~/.claude (backs up settings.json)
  uninstall-claude    remove only what install-claude added
  install-codex       write the reviewer Codex config in the relay's codex_home
  hook-stop | hook-session-end | mcp   (called by Claude Code / Codex)"""


def session_env():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        sys.exit("relay: run this from inside a Claude Code session (CLAUDE_CODE_SESSION_ID is not set)")
    return sid


def default_label(cwd):
    r = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True)
    return r.stdout.strip() or pathlib.Path(cwd).name


def project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def transcript_path(cwd, sid):
    """Where Claude Code keeps the transcript of session `sid` started in `cwd`: under its configuration folder,
    CLAUDE_CONFIG_DIR when set (Claude Code passes it on to the commands it runs), else ~/.claude."""
    config = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
    return config / "projects" / "".join(c if c.isalnum() else "-" for c in cwd) / f"{sid}.jsonl"


def parse_role(args):
    """Split an optional `--as <role>` out of the arguments; what remains is the label."""
    if "--as" not in args:
        return None, list(args)
    i = args.index("--as")
    if i + 1 >= len(args) or args[i + 1].startswith("-"):
        sys.exit("relay: --as needs a role, e.g. --as verifier")
    return args[i + 1], args[:i] + args[i + 2:]


def link(args, once=False):
    sid = session_env()
    cwd = project_dir()
    role, args = parse_role(args)
    label = " ".join(args).strip()
    db = store.connect()
    if once:
        return review_once(db, sid, cwd, label, role)
    label = label or default_label(cwd)
    db.execute("INSERT OR REPLACE INTO links (session_id, label, role, cwd, once, skip_next, created)"
               " VALUES (?, ?, ?, ?, 0, 1, ?)", (sid, label, role, cwd, store.now()))
    store.event(db, "link", f"{sid} -> {label}")
    reply_with_conversation(db, sid, label, cwd, f"Linked this session to '{label}'",
                            then="Each turn from now on goes to Codex review.")


def review_once(db, sid, cwd, label, role):
    """/relay-review: have the Stop hook of the command's own turn forward the session's last reply, once. A
    session that is already linked keeps its link: the one-off rides on the link's row, under the label and role
    it names (the link's own, for whichever it doesn't name), and the hook clears it after forwarding. Refused,
    writing nothing, when there is no reply to forward: Claude Code then runs no turn, so no Stop hook follows."""
    transcript = transcript_path(cwd, sid)
    if not transcript.is_file():
        sys.exit(f"relay: this session's transcript wasn't found at {transcript}. Nothing was forwarded.")
    message = claude_hooks.last_reply(transcript, MARKER)
    if not message:
        sys.exit("relay: no reply to forward. This session has no Claude reply yet, other than replies to the "
                 "relay's own commands. Nothing was forwarded.")
    own = db.execute("SELECT * FROM links WHERE session_id=? AND once=0", (sid,)).fetchone()
    sent_as = stays = ""
    if own:
        label, role = label or own["label"], role or own["role"]
        db.execute("UPDATE links SET skip_next=1, once_message=?, once_label=?, once_role=? WHERE session_id=?",
                   (message, label, role, sid))
        stays = f"this session stays linked to '{own['label']}'"
        if (label, role) != (own["label"], own["role"]):
            sent_as = f" as {store.display_name(role, own['cwd'], sid)}"
            stays += (f" as {store.display_name(own['role'], own['cwd'], sid)}, and its later replies go there as "
                      "before")
        if os.path.isdir(own["cwd"]):  # the conversation's project, chosen as the Stop hook will choose it
            cwd = own["cwd"]
    else:
        label = label or default_label(cwd)
        db.execute("INSERT OR REPLACE INTO links (session_id, label, role, cwd, once, skip_next, once_message, created)"
                   " VALUES (?, ?, ?, ?, 1, 1, ?, ?)", (sid, label, role, cwd, message, store.now()))
    store.event(db, "link:once", f"{sid} -> {label}")
    reply_with_conversation(db, sid, label, cwd,
                            f"Forwarding the previous reply for '{label}'{sent_as} when this turn ends", also=stays)


def reply_with_conversation(db, sid, label, cwd, done, also="", then=""):
    """Create the ticket's conversation now (empty: nothing is queued), so it can be opened straight away, and
    print the command's one-line reply. Its first sentence holds the clauses `done`, how to open the conversation
    and `also`; the sentence `then` follows. The link is already recorded; if Codex can't be reached, the Stop
    hook creates the conversation at the first hand-off."""
    def reply(*clauses, last=""):
        print(" ".join(filter(None, ("; ".join(filter(None, clauses)) + ".", then, last))))
    try:
        intake.ensure_thread(db, store.load_config(), label, cwd)
    except (OSError, subprocess.SubprocessError, codexrpc.CodexError, KeyError, ValueError) as e:
        # also a malformed config or an unexpected Codex reply: the user gets the plain line, not a traceback
        store.event(db, "link:no_conversation", f"{sid} -> {label}")  # the reason is in the reply
        reason = " ".join(str(e).split())  # one line: the command file has Claude echo the line verbatim
        return reply(done, also, last=f"The Codex conversation couldn't be created yet ({reason}); it will be retried "
                                      "when the first turn is forwarded.")
    # The hint and what happens to an existing link go in the first sentence: Claude is told to echo this line, but
    # sometimes keeps only the start. The hint is worded for the user: Claude has taken a bare "open it now: relay
    # review …" as its own task and run it.
    reply(done, f'open its Codex conversation now from your own terminal: relay review "{label}"', also)


def unlink():
    sid = session_env()
    db = store.connect()
    db.execute("DELETE FROM links WHERE session_id=?", (sid,))
    store.event(db, "unlink", sid)
    print("Unlinked: turns from this session are no longer forwarded.")


def resolve_review(db, label, cwd):
    """Pick the conversation for `relay review LABEL` run from `cwd`: ("open", [row]) for the current project's,
    else the only match; ("choose", rows) when several projects have the label; ("none", [])."""
    rows = db.execute("SELECT * FROM threads WHERE label=? ORDER BY created", (label,)).fetchall()
    here = store.project_of(cwd)
    mine = [r for r in rows if r["project"] == here]
    if mine:
        return "open", mine
    if len(rows) == 1:
        return "open", rows
    return ("choose" if rows else "none"), rows


def review(args):
    label = " ".join(args).strip()
    db = store.connect()
    cfg = store.load_config()
    kind, rows = resolve_review(db, label, os.getcwd())
    if kind == "none":
        known = [f"{r['label']} ({r['project']})" for r in db.execute("SELECT * FROM threads ORDER BY created DESC")]
        sys.exit(f"relay: no Codex conversation for '{label}'. Known: {', '.join(known) or 'none'}")
    if kind == "choose":
        sys.exit(f"relay: '{label}' is a conversation in several projects. Run this from one of them:\n"
                 + "\n".join(f"  {r['project']}" for r in rows))
    row = rows[0]
    codexrpc.ensure_daemon(cfg)
    codexrpc.ensure_loaded(cfg, row["thread_id"])
    os.chdir(row["cwd"])
    os.execvpe(cfg["codex_bin"], [cfg["codex_bin"], "resume", row["thread_id"]], codexrpc.codex_env(cfg))


def status():
    db = store.connect()
    for h in db.execute("SELECT * FROM handoffs ORDER BY id DESC LIMIT 20"):
        drafts = []  # each draft answering this report (its source), newest first, with where it went
        for d in db.execute("SELECT * FROM drafts WHERE handoff_id=? AND status!='replaced' ORDER BY id DESC",
                            (h["id"],)):
            r = store.handoff(db, d["recipient_handoff_id"]) if d["recipient_handoff_id"] else None
            drafts.append(f"D{d['id']}:{d['status']}" + (f" -> {store.session_name(r)} H{r['id']}" if r else ""))
        print(f"H{h['id']:<4} {h['label'][:24]:<24} {h['status']:<14} {store.session_name(h)} · "
              f"{', '.join(drafts) or 'no draft'}{' · ' + h['reason'] if h['reason'] else ''}")


RELAY = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bin", "relay"))

COMMANDS = {
    "relay-link.md": ("Link this session to a ticket for Codex review", "[--as <role>] [label]", "link"),
    "relay-unlink.md": ("Stop forwarding this session to Codex review", "", "unlink"),
    "relay-review.md": ("Forward the previous reply to Codex review once", "[--as <role>] [label]", "review-once"),
}
MARKER = "<!-- cc-codex-relay managed command: safe to update or remove with relay uninstall-claude -->"
HOOKS = {"Stop": (f"{RELAY} hook-stop", 14400), "SessionEnd": (f"{RELAY} hook-session-end", 10)}
CAP_KEY = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"


def claude_home():
    return pathlib.Path(os.environ.get("RELAY_CLAUDE_HOME") or pathlib.Path.home() / ".claude")


def is_relay_hook(h):
    return h.get("command") in {cmd for cmd, _ in HOOKS.values()}


def strip_relay_hooks(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", []) if not is_relay_hook(h)]
            if kept:
                groups.append({**g, "hooks": kept})
            elif not g.get("hooks"):
                groups.append(g)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]


def backup_settings(path):
    """Copy settings.json to a new timestamped file; never overwrite an earlier backup."""
    stem = path.with_name(f"settings.json.relay-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    backup, n = stem, 1
    while backup.exists():
        backup = stem.with_name(f"{stem.name}-{n}")
        n += 1
    shutil.copy2(path, backup)
    return backup


def install_claude():
    home = claude_home()
    commands = home / "commands"
    # Refuse before changing anything if a command file we would write already exists and isn't ours.
    foreign = [commands / n for n in COMMANDS if (commands / n).exists() and MARKER not in (commands / n).read_text()]
    if foreign:
        sys.exit("relay: not installing. These files exist and were not written by the relay:\n  "
                 + "\n  ".join(map(str, foreign)) + "\nMove or rename them, then run install-claude again.")
    settings_path = home / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if settings_path.exists():
        backup = backup_settings(settings_path)
        print(f"Backed up {settings_path} -> {backup}")
    state_path = store.relay_home() / "claude-install.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    env = settings.setdefault("env", {})
    if "cap_before" not in state:  # first install: remember what was there so uninstall can restore it
        state["cap_before"] = env.get(CAP_KEY)
    strip_relay_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event, (command, timeout) in HOOKS.items():
        hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
    # Claude Code ends a turn after 8 consecutive Stop-hook blocks and ignores the last draft. Every Send
    # in the review cycle is one such block, so raise the cap well above any realistic review loop.
    env[CAP_KEY] = "1000"
    commands.mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    store.relay_home().mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))
    for name, (desc, hint, sub) in COMMANDS.items():
        args = " $ARGUMENTS" if hint else ""
        (commands / name).write_text(
            f"---\ndescription: {desc}\nargument-hint: {hint}\nallowed-tools: Bash({RELAY} {sub}:*)\n---\n"
            # Claude echoes this to the user. Run in live tests, it has copied an instruction or marker next to the
            # output, and run the relay review command the output names; so the output is fenced in a tag.
            f"{MARKER}\n\nReply with only the text inside <relay-reply>, copied exactly, without the tags. Don't run "
            "any command it names: that is for the user, in their own terminal.\n\n"
            f"<relay-reply>\n!`{RELAY} {sub}{args}`\n</relay-reply>\n")
    print(f"Installed relay hooks in {settings_path} and /relay-link, /relay-unlink, /relay-review in {commands}")


def uninstall_claude():
    home = claude_home()
    settings_path = home / "settings.json"
    state_path = store.relay_home() / "claude-install.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
        backup = backup_settings(settings_path)
        print(f"Backed up {settings_path} -> {backup}")
        strip_relay_hooks(settings)
        env = settings.get("env", {})
        if env.get(CAP_KEY) == "1000":  # only undo our value; a value the user changed since is left alone
            if state.get("cap_before") is None:
                env.pop(CAP_KEY, None)
            else:
                env[CAP_KEY] = state["cap_before"]
        if "env" in settings and not settings["env"]:
            del settings["env"]
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    removed = []
    for name in COMMANDS:
        f = home / "commands" / name
        if f.exists() and MARKER in f.read_text():
            f.unlink()
            removed.append(name)
    state_path.unlink(missing_ok=True)
    print(f"Removed relay hooks from {settings_path}; removed commands: {', '.join(removed) or 'none'}")


def system_python():
    """The interpreter Codex should run the relay with: never a project virtualenv that happens to be
    first on PATH (the relay is stdlib-only)."""
    if sys.prefix != sys.base_prefix:  # running inside a venv: use the Python it was created from
        for name in (f"python{sys.version_info.major}.{sys.version_info.minor}", "python3"):
            candidate = pathlib.Path(sys.base_prefix) / "bin" / name
            if candidate.exists():
                return str(candidate)
    return sys.executable


def install_codex():
    cfg = store.load_config()
    home = pathlib.Path(cfg["codex_home"])
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    existing = path.read_text() if path.exists() else ""
    begin, finish = "# --- cc-codex-relay (managed)", "# --- end cc-codex-relay ---"
    lines, skip = [], False
    for line in existing.splitlines():
        if line.startswith(begin):
            skip = True
        elif line.startswith(finish):
            skip = False
        elif not skip:
            lines.append(line)
    user = "\n".join(lines).strip()
    py = system_python()
    top = f"{begin} top-level ---\nthread_unload_delay_secs = 31536000\n{finish}\n"
    tables = (f"{begin} tables ---\n"
              f"[mcp_servers.relay]\ncommand = \"{py}\"\nargs = [\"{RELAY}\", \"mcp\"]\n"
              f"env = {{ RELAY_HOME = \"{store.relay_home()}\" }}\nstartup_timeout_sec = 60\n\n"
              "[mcp_servers.relay.tools.propose_reply]\napproval_mode = \"approve\"\n\n"
              "[mcp_servers.relay.tools.show_menu]\napproval_mode = \"approve\"\n\n"
              "[mcp_servers.relay.tools.no_reply_menu]\napproval_mode = \"approve\"\n")
    for project in cfg["personas"]:
        tables += f"\n[projects.\"{project}\"]\ntrust_level = \"trusted\"\n"
    tables += f"{finish}\n"
    # TOML: top-level keys must precede every table, so the managed key goes first and the
    # managed tables last, with the user's own settings in between.
    path.write_text(top + (user + "\n" if user else "") + tables)
    os.chmod(path, 0o600)
    print(f"Wrote reviewer Codex config {path}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit(USAGE)
    cmd, args = argv[0], argv[1:]
    if cmd == "link":
        link(args)
    elif cmd == "review-once":
        link(args, once=True)
    elif cmd == "unlink":
        unlink()
    elif cmd == "review":
        review(args)
    elif cmd == "status":
        status()
    elif cmd == "install-claude":
        install_claude()
    elif cmd == "uninstall-claude":
        uninstall_claude()
    elif cmd == "install-codex":
        install_codex()
    elif cmd == "hook-stop":
        sys.exit(claude_hooks.stop())
    elif cmd == "hook-session-end":
        sys.exit(claude_hooks.session_end())
    elif cmd == "mcp":
        mcp_server.main()
    else:
        sys.exit(USAGE)
