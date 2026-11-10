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
| 21 | Copied draft, 8 KB or more | After the isolated setup was corrected to hold only this checkout's hooks, the READY turn is filed as one hand-off. Codex's real `propose_reply` call with `copy: "all"` and additions builds a 9 KB draft with CRLF and non-ASCII text from the stored report (replaced by a known text so its bytes are fixed). The stored draft, the draft file and Claude's transcript are byte-identical. The `anyOf` schema for `copy` was accepted by Codex for the `"all"` form; the selection-list form was not tried live. |
| 22 | Refreshing an existing conversation (investigation) | Created with the previous commit's instructions. **Process (observed):** the tool server was kept on a next turn, replaced by reopening the window after a 15 s wait, and replaced by each daemon restart. **Recorded:** the developer messages never contained the new section. **Model-reported:** Codex's answers about its tools matched the process evidence (an answer that didn't would make the verdict INCONCLUSIVE); in one earlier run a probe made straight after the window opened still showed the old list. This does **not** pass the open requirement to update an existing conversation's operating instructions; no tested route exists. |
| 23 | Refreshing an existing conversation for the no-reply path (#3) | PASS in `20261109-040117`, installed store unchanged. Tool server kept on a next turn, replaced by reopening the window after 15 s. Real Codex, asked by the harness, called `no_reply_menu`: exactly Discuss / Keep waiting / Finish, no Send or Copy; assessment stored with no draft; after Discuss the recorded result had "still held", "Finish first", "show_menu"; the source stayed held. Creation-time instructions did not gain the new section (#2 unresolved). Codex was asked to make the call; unprompted choice not tested. |
| 24 | One-off review in a linked session (#5) | PASS in `20261110-222302`, installed store unchanged. After `/relay-link`'s acknowledgement, `/relay-review` forwarded the reply from before the link, not the acknowledgement, and said the session stays linked; the link was unchanged. After Esc released the hold, the next ordinary reply was reported under the same label and role, in the same conversation. A different label or role, the refusals and the background-task skip were not run live. |
| 12 | Real VS Code terminal | The full cycle works from a real VS Code terminal. *By hand.* |

**Open for #2:** updating the operating instructions of an existing conversation has no tested route (test 22 is an investigation of it, not a pass), and the effect of the short per-hand-off guidance on an existing conversation was not tested.

**Known gap:** Windows toasts were not seen on the test machine, because Windows had toasts switched off for the PowerShell sender the relay uses (see DESIGN.md, Build findings 2026-10-31). Notifications are still written to the log.

Not re-run live: a recipient that stopped waiting before Send, a missing or wrong report number, and the same label in two projects. Unit tests cover them.
