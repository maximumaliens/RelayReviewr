"""Isolated acceptance environment on the dev box. Safe to re-run.

  python3 devenv/setup.py      build or refresh the environment, then report the sign-ins it still needs

Everything lives in two folders, and nothing the user has installed is read or changed:
  ACCEPT (~/.local/state/cc-codex-relay-accept, or $RELAY_ACCEPT_ROOT), kept between runs:
    relay/          relay state folder (RELAY_HOME) for every dev, test and menu-server process
    codex-home/     development reviewer Codex home, signed in once with `codex login`
    claude-config/  Claude configuration for test sessions (CLAUDE_CONFIG_DIR), signed in once
  TMP (/tmp/relay-accept, or $RELAY_ACCEPT_TMP), rebuilt for each test:
    repo/           the throwaway repository; the only project with a persona, so the only one Codex trusts
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELAY = ROOT / "bin" / "relay"
sys.path.insert(0, str(ROOT / "tests" / "acceptance"))
import isolation  # noqa: E402

ACCEPT = pathlib.Path(os.environ.get("RELAY_ACCEPT_ROOT") or pathlib.Path.home() / ".local/state/cc-codex-relay-accept")
TMP = pathlib.Path(os.environ.get("RELAY_ACCEPT_TMP") or "/tmp/relay-accept")
RELAY_HOME = ACCEPT / "relay"
CODEX_HOME = ACCEPT / "codex-home"
CLAUDE_CONFIG_DIR = ACCEPT / "claude-config"
REPO = TMP / "repo"
PERSONA = ROOT / "devenv" / "test-persona.md"
TITLE_PREFIX = "[relay test] "
TMUX_SOCKET = "relay-accept"  # a tmux server of its own, so the harness never sees the user's sessions

# Inherited variables that would point a child at the installed setup or at the Claude session running the harness.
DROP_PREFIXES = ("CLAUDE", "ANTHROPIC", "CODEX_", "RELAY_", "TMUX")


def env():
    """Environment for every process the harness starts: the caller's, minus anything Claude-, Codex- or
    relay-specific, plus the isolated locations."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(DROP_PREFIXES)}
    e.update(RELAY_HOME=str(RELAY_HOME), CODEX_HOME=str(CODEX_HOME), CLAUDE_CONFIG_DIR=str(CLAUDE_CONFIG_DIR))
    return e


def check_paths():
    for p in (ACCEPT, TMP, RELAY_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, REPO):
        isolation.refuse_installed(p)


def relay(*args, **extra):
    subprocess.run([sys.executable, str(RELAY), *args], env={**env(), **extra}, check=True,
                   stdout=subprocess.DEVNULL)


def make_repo():
    if (REPO / ".git").exists():
        return
    REPO.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "trial"]):
        subprocess.run(cmd, cwd=REPO, check=True)
    (REPO / "README").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=REPO, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=REPO, check=True)


def write_relay_config():
    RELAY_HOME.mkdir(parents=True, exist_ok=True)
    (RELAY_HOME / "config.json").write_text(json.dumps({
        "codex_home": str(CODEX_HOME),
        "live_log": str(TMP / "live.log"),
        "personas": {str(REPO): str(PERSONA)},
        "notify_title_prefix": TITLE_PREFIX,
    }, indent=1) + "\n")


def write_codex_config():
    """Cheap model for tests, no update notice (it covers the window the harness reads), then the relay-managed
    block (relay tool server, unload delay, trust for REPO)."""
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    (CODEX_HOME / "config.toml").write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "low"\n'
                                            "check_for_update_on_startup = false\n\n"
                                            "[tui]\nscreen_reader_detection_done = true\n")
    relay("install-codex")


RELAY_HOOK_COMMAND = re.compile(r".*/bin/relay hook-(stop|session-end)")


def drop_relay_hooks_of_any_checkout(settings):
    """Remove every relay hook, whichever checkout's bin/relay it names, from the isolated Claude settings, so the
    install that follows leaves only this checkout's. The production installer removes only its own checkout's
    commands, so a hook left by another checkout (the development checkout was moved or renamed) would run
    beside this one and file each report twice. Other hooks and settings are kept."""
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", []) if not RELAY_HOOK_COMMAND.fullmatch(h.get("command") or "")]
            if kept or not g.get("hooks"):
                groups.append({**g, "hooks": kept} if g.get("hooks") else g)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]


def write_claude_config():
    """The development relay's hooks and commands only, auto mode, and no first-run screens for REPO."""
    CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    settings_file = CLAUDE_CONFIG_DIR / "settings.json"
    if settings_file.exists():
        existing = json.loads(settings_file.read_text())
        drop_relay_hooks_of_any_checkout(existing)
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")
    relay("install-claude", RELAY_CLAUDE_HOME=str(CLAUDE_CONFIG_DIR))
    for backup in CLAUDE_CONFIG_DIR.glob("settings.json.relay-backup-*"):  # one per reset; nothing to keep
        backup.unlink()
    path = CLAUDE_CONFIG_DIR / "settings.json"
    s = json.loads(path.read_text())
    s.setdefault("env", {})["RELAY_HOME"] = str(RELAY_HOME)  # hooks and /relay-link can't fall back to the installed store
    s.setdefault("permissions", {})["defaultMode"] = "auto"
    path.write_text(json.dumps(s, indent=2) + "\n")
    state = CLAUDE_CONFIG_DIR / ".claude.json"
    d = json.loads(state.read_text()) if state.exists() else {}
    d.update({"hasCompletedOnboarding": True, "theme": "dark", "autoUpdates": False})
    proj = d.setdefault("projects", {}).setdefault(str(REPO), {})
    proj.update({"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True})
    state.write_text(json.dumps(d, indent=2))


def missing_sign_ins():
    """The sign-ins the user still has to do (HUMAN pauses); credentials are never copied in."""
    out = []
    r = subprocess.run(["codex", "login", "status"], env=env(), capture_output=True, text=True)
    if r.returncode:
        out.append(f"Codex reviewer:  CODEX_HOME={CODEX_HOME} codex login")
    if not (CLAUDE_CONFIG_DIR / ".credentials.json").exists():
        out.append(f"Claude test sessions:  cd {REPO} && CLAUDE_CONFIG_DIR={CLAUDE_CONFIG_DIR} claude   then /login, then /exit")
    return out


def setup():
    check_paths()
    TMP.mkdir(parents=True, exist_ok=True)
    make_repo()
    write_relay_config()
    write_codex_config()
    write_claude_config()


def clear_test_state():
    """Fresh relay state and throwaway repo for the next test; the config and sign-ins are kept."""
    check_paths()
    for p in ("relay.db", "relay.db-wal", "relay.db-shm", "notifications.log", "test-hide-tools"):
        (RELAY_HOME / p).unlink(missing_ok=True)
    shutil.rmtree(RELAY_HOME / "drafts", ignore_errors=True)
    shutil.rmtree(REPO, ignore_errors=True)
    shutil.rmtree(TMP / "repo-wt", ignore_errors=True)  # a worktree of REPO left by an interrupted test 16


if __name__ == "__main__":
    setup()
    todo = missing_sign_ins()
    print(f"acceptance environment ready: RELAY_HOME={RELAY_HOME} CODEX_HOME={CODEX_HOME} "
          f"CLAUDE_CONFIG_DIR={CLAUDE_CONFIG_DIR} repo={REPO}")
    if todo:
        print("Sign-ins still needed (run each yourself, in a terminal):\n  " + "\n  ".join(todo))
        sys.exit(2)
