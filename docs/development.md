# Development and tests

How to change the relay and check it. For using it, see the [README](../README.md). Design rulings are in [DESIGN.md](../DESIGN.md); the rules a code reviewer checks are in [CODING_STANDARDS.md](../CODING_STANDARDS.md).

## Where to work

Develop and test in a separate checkout or worktree (e.g. `~/tools/cc-codex-relay-dev`), never in the installed checkout that your own Claude hooks run. Run its relay by path (`<worktree>/bin/relay`), never through the `relay` on `PATH`.

Take care with your own Claude sessions while you develop: a session linked to the installed relay writes to the installed store every time a turn ends.

## Unit tests

`python3 -m unittest discover -s tests -p 'test_*.py'`. They need no sign-ins and use fakes for Claude and Codex; `tests/support.py` holds the shared test seams. CI runs them on every push and pull request. To also check that the relay's wording names no particular person or project, pass the names as a regex, which keeps them out of the repository: `RELAY_PRIVATE_NAMES='alice|acme' python3 -m unittest discover -s tests -p 'test_*.py'`.

## Acceptance tests (real Claude and Codex)

The acceptance tests run in an isolated environment that `python3 devenv/setup.py` builds and refreshes (safe to re-run). Nothing the user has installed is read or changed:

| Part | Where | Isolation |
|---|---|---|
| Relay state | `~/.local/state/cc-codex-relay-accept/relay` | Its own config: the development reviewer Codex home, a test persona (`devenv/test-persona.md`) mapped only to the throwaway repo, and notification titles starting `[relay test]`. |
| Reviewer Codex home | `…-accept/codex-home` | Its own login, daemon and trust list (only the throwaway repo). |
| Claude configuration | `…-accept/claude-config` (`CLAUDE_CONFIG_DIR`) | Only the development relay's hooks and `/relay-*` commands, in auto mode. Settings, commands, transcripts, credentials and `.claude.json` all live there ([docs](https://code.claude.com/docs/en/env-vars.md)), so the user's `~/.claude` hooks don't run. |
| Repository | `/tmp/relay-accept/repo` | Throwaway, rebuilt for every test. |
| tmux | socket `relay-accept` | The harness never sees or kills the user's tmux sessions. |

Override the two roots with `RELAY_ACCEPT_ROOT` and `RELAY_ACCEPT_TMP`. Setup refuses any path inside the installed relay state, `~/.codex-reviewer`, `~/.codex` or `~/.claude`.

1. `python3 devenv/setup.py`. The first time, it lists two sign-ins for the user to run in their own terminal. Credentials are never copied from the installed setup.
   - `CODEX_HOME=~/.local/state/cc-codex-relay-accept/codex-home codex login`
   - `cd /tmp/relay-accept/repo && CLAUDE_CONFIG_DIR=~/.local/state/cc-codex-relay-accept/claude-config claude`, then `/login`, then `/exit`.
2. `python3 tests/acceptance/run.py [n ...]` runs acceptance tests 1–11, E1 (12), E2 (13) and the cross-session checks 14–18 (a report queued behind an open menu, two sessions on one ticket, standing context and persona, permission handling (3b), long Send to … labels) and #9's checks 19–20 (`/relay-link` and `/relay-review` with the development Codex daemon stopped: the conversation exists and opens before any hand-off, nothing is queued, Claude shows the reply exactly and runs no command) against real interactive Claude and Codex windows, and writes `results/<run-id>/` (not tracked by git; [acceptance-results.md](acceptance-results.md) summarises past runs). With `ACCEPT_HUMAN=1`, tests 14, 17 and 18 pause for a person: the log prints a read-only `tmux -L relay-accept attach -r -t <window>` and waits until what they saw is written to `/tmp/relay-accept/continue`, which goes into the evidence. Without it, those looks are skipped and the evidence says so. Before each test it stops the development Codex daemon (killing it if a menu holds a graceful stop), so a turn left over from the previous test can't act on the next test's store. If Codex is waiting out a rate limit (HTTP 429, read from the development Codex home's log), a wait is extended to the end of that back-off, and the test's evidence says so.
3. Each run snapshots the installed relay's store (`~/.local/state/cc-codex-relay/relay.db`, read-only) first and compares it row by row at the end. A new, removed or replaced row was written by the run if, before or after the change, it carries one of the run's test session IDs, test labels or a path inside an acceptance root in the columns that name a session, ticket or folder, or it belongs to a hand-off or Codex thread that does. Message text (Claude's replies, drafts, notices) doesn't count. For such a row, `summary.md` starts with **INVALID run** and shows the row. Each `t<NN>.json` records the changes so far under `installed_store_changes`.
   - Real Claude sessions can be used and closed during a run. The installed hooks write their rows to the installed store, and those rows carry none of the run's marks. `summary.md` and `t<NN>.json` list them under **Not attributed to this run**, with their contents, and they don't make the run invalid. Nothing is dropped from that list, so a leak under an unexpected ID still shows there: check it before trusting a run.
   - `run.py` exits 0 for a valid run, whatever the tests' verdicts; 1 only for an INVALID run; 2 when the run couldn't start or finish (missing sign-ins, a refused path, a runner error), with the reason on stderr.

4. Design test 12 (the cycle from a real VS Code terminal) is done by hand. Run `python3 tests/acceptance/manual.py start` first and `python3 tests/acceptance/manual.py finish` afterwards: it applies the same installed-store check and writes `results/<run-id>-manual/`. Start Claude for it with `CLAUDE_CONFIG_DIR=~/.local/state/cc-codex-relay-accept/claude-config claude` and check its status line says auto mode; a plain `claude` runs the installed hooks.

The development Codex home turns off Codex's update check: an "Update available" screen covers the window the harness reads.

## Keeping commits free of personal details

This repository is public. Before you commit:

- **Commit under GitHub's no-reply address.** In this repository, set `git config user.email <id>+<login>@users.noreply.github.com` (your ID and login are under GitHub Settings → Emails). Also turn on "Keep my email addresses private" and "Block command line pushes that expose my email" there.
- **Keep commit messages generic.** No personal or project names, home-folder paths, machine names or real ticket labels. A `Co-Authored-By` line for a coding tool is fine; leave out session links (such as `Claude-Session:` lines) that point at your own sessions.
- **Keep personal configuration out of the repository.** Personas, standing context and project paths belong in `~/.local/state/cc-codex-relay/config.json` and in files you keep elsewhere. Only the generic `persona/example.md` is tracked.
- **Don't commit acceptance evidence.** `results/` is ignored because transcripts can quote your persona, paths and sessions. Summarise results instead.
- **Commit times show your time zone.** To hide it, commit with `TZ=UTC git commit …`.
- **Check before pushing:** run `gitleaks git .` for secrets, and the unit tests with `RELAY_PRIVATE_NAMES` set to your own names.
