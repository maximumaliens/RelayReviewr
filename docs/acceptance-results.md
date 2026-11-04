# Acceptance results (summary)

The acceptance tests in `tests/acceptance/` drive real, interactive Claude Code and Codex windows in an isolated environment (see [development.md](development.md)). Each run writes its raw evidence to `results/<run-id>/`, which is not kept in this repository.

On the code this repository was published from, every automated check below passed in the isolated environment, with the installed relay's store unchanged. The checks marked *by hand* were done by a person watching.

| # | Check | What it shows |
|---|---|---|
| 1 | Exact text | A 4 KB draft with quotes, backticks, `$`, `\`, non-ASCII and emoji reaches Claude byte-for-byte. |
| 2 | Correct session | Two tickets waiting at once, and two sessions on one ticket: each Send reaches only the chosen session; the source stays held. |
| 3 | Ordinary execution | An approved instruction (create a file, commit it) is carried out, not just discussed. |
| 3b (17) | Permission handling | An instruction needing a permission the session lacks still raises Claude's own prompt; nothing answers it. *By hand.* |
| 4 | No duplicates | A second Send of a sent draft is refused. An interrupted send ends as `unknown`, offers no Send, and Claude received nothing. |
| 5 | Visible failure | A killed hook, a changed hook format and an ended session each give a visible reason; nothing is sent. |
| 6 | Esc | Esc in Claude releases the wait; Send is withdrawn. |
| 7 | Open-window cycle | Send, Claude works, and the next review appears in the same open Codex window with nothing typed. |
| 8 | Menu survival | A graceful daemon restart waits while a menu is open. After the daemon is killed, reopening shows the menu and Send delivers. |
| 9 | Escalation prompts | Under `on-request`, Codex stayed in the read-only sandbox and asked for no escalation (one sample). |
| 10 | Thread-loaded guard | With the daemon stopped, the relay restarts it and loads the conversation; opening a window never starts an assessment. |
| 11 | No-draft detector | When Codex ends a turn without a draft, the user is notified and Claude stays held. |
| E1 | Claude killed outright | The hand-off is released, the open menu withdrawn, and the hook exits. |
| E2 | 10 consecutive Sends | All are delivered and acted on (this needs the Stop-hook limit `install-claude` sets). |
| 14 | Report queued behind an open menu | The report waits, its session stays held, the notice is raised, and after Discuss it runs in a new Codex turn. |
| 15 | Two sessions on one ticket | A verifier's draft reaches only the implementer; a later Send reaches only the verifier. |
| 16 | Standing context and persona | The first report of a new conversation carries the standing context, from the main checkout and a worktree; the persona applies. |
| 18 | Long Send to … labels | Long role names stay readable at 220 and 80 columns. *By hand.* |
| 19–20 | Conversation at link time | `/relay-link` and `/relay-review` create the conversation before any report; nothing is queued, and Claude shows the reply exactly. |
| 12 | Real VS Code terminal | The full cycle works from a real VS Code terminal. *By hand.* |

**Known gap:** Windows toasts were not seen on the test machine, because Windows had toasts switched off for the PowerShell sender the relay uses (see DESIGN.md, Build findings 2026-10-31). Notifications are still written to the log.

Not re-run live: a recipient that stopped waiting before Send, a missing or wrong report number, and the same label in two projects. Unit tests cover them.
