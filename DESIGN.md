# cc-codex-relay — design record

Status: design agreed with the user in a design interview ("grilling"), 2026-10-19/20, and built. Cross-session delivery (see that section) was agreed in a second session on 2026-10-25, scope confirmed the same day, and built since (#1–#8).

Claim labels: **[tested]** means seen working in a live trial. **[source]** means read in docs or source code but not run. **[unverified]** means still open.

## Goal

Remove the manual copying between Claude Code and a Codex review conversation. The user stays in control: nothing goes back to Claude Code unless they pick **Send**.

## The normal cycle

1. A Claude Code turn ends in a session linked to a ticket. Its Stop hook files the turn's final output as a handoff, then waits for the user's decision.
2. The relay queues the handoff into that ticket's Codex conversation. Codex assesses it automatically, checking claims against the repo read-only, and drafts a return prompt.
   - If the Codex window is open, the assessment runs live in it.
   - If the window is closed, the assessment runs in the background. Neither opening the window nor typing triggers it.
3. A plain Windows notification appears, for example "ABC-42 triage: review ready".
4. The user opens the conversation, or is already in it. The menu appears inside Codex: **Send / Discuss / Keep waiting / Finish without sending**.
5. After Send, Claude carries on. When its next turn ends, step 2 repeats in the same conversation. The user doesn't have to exit Codex or type anything.

## Components

The code lives in its checkout (e.g. `~/tools/cc-codex-relay/`) and its state under `~/.local/state/cc-codex-relay/` (`RELAY_HOME`). Configuration lives at user level in `~/.claude` and in a dedicated Codex home. Nothing goes into the projects being reviewed.

### Claude side (user-level `~/.claude`)
- `/relay-link <label>` records session ID → ticket label. With no argument it suggests the git branch name. `/relay-unlink` stops forwarding. `/relay-review [label]` forwards one turn from any session, linked or not; that session then waits like a linked one. The names are prefixed so they can't clash with existing commands or skills: Claude Code already has a `review` skill.
- **Conversation at link time** (#9, 2026-11-01). `/relay-link` and `/relay-review` create the ticket's conversation for the session's project and label while the command runs (`intake.ensure_thread`: same daemon start, the per-conversation lock, persona and thread-loaded guard as the Stop hook), so `relay review` works at once. Nothing is queued, so opening it triggers no assessment, and standing context still rides on the first hand-off. If Codex can't be reached the link is still recorded, the reply says so, and the first forwarded turn's Stop hook creates the conversation as before. Upgrade-safe: a hook started on older code finds the conversation created at link time through the same lookup, and link time writes nothing it polls (standing context is recorded only when a hand-off is queued).
- **Install safety:** `install-claude` backs up `settings.json` (timestamped) before each change. It refuses, changing nothing, if a `relay-*.md` command file without the relay's marker already exists. It removes only its own hook entries, matched exactly. `uninstall-claude` removes only what it added and restores the stop-hook cap to its previous value. It leaves a cap value the user changed after install, and every other setting, as they are.
- **Stop hook** (`timeout: 14400`, i.e. 4 h):
  - It ignores sessions that aren't linked. Subagent turns (SubagentStop) are never forwarded, nor are turns still waiting on background tasks.
  - It files the handoff and waits.
  - On **Send** it prints `{"decision":"block","reason":"<exact stored draft>"}`. Claude receives this as `Stop hook feedback:\n<draft>`: an instruction the user approved, delivered through a hook. It is a valid work instruction, but it is not a message the user typed, and it doesn't satisfy a separate permission gate or answer a dialog (ruling 2026-10-25). [tested: byte-identical; an ordinary instruction (create a file and commit) was carried out]
  - On **Finish without sending**, on a 4 h timeout, or when the user presses Esc in Claude [tested: Esc interrupts the turn and ends the hook], the turn ends and nothing is sent.
  - Claude Code kills a hook when its `timeout` expires, and the default is 600 s. So the 14400 value must be set.
- A newer handoff from the same session replaces older ones that haven't been reviewed (Q12). The older ones stay readable, and no separate review is asked for.

### Relay (`~/tools/cc-codex-relay/`)
- A SQLite store holds handoffs, drafts, the ticket → Codex thread map, and draft states: `ready → sending → sent | failed | unknown`.
- **Codex intake is `codex queue --thread <id> --message <handoff>` only.**
  - A queued message starts a turn straight away when the thread is loaded and idle, whether or not a window is open. It waits if a turn is already running. [tested]
  - `codex exec resume` is **not** used:
    - it fails while a window is open, because Codex allows only one writer per thread [tested];
    - it pulls pending queued items into its own turn [source/spike];
    - it resets the sandbox to workspace-write [spike].
- **Thread-loaded guard.** This is the relay's critical job.
  - An unloaded thread with a queued item would run the assessment the moment the user opens it. That breaks the rule that opening must not trigger an assessment.
  - Threads unload after 30 min idle by default (`thread_unload_delay_secs` = 1800). [source/spike]
  - The relay raises that delay in the reviewer home's config.
  - After each queue it confirms the daemon is up and the thread is loaded, using `thread/loaded/list` and `thread/resume` over the daemon control socket.
  - If it can't confirm that, it marks the handoff failed and notifies the user. It never leaves a pending item that opening the thread would fire.
- `codex queue` exits 0 even when the daemon is stopped [spike]. So the relay records the printed queue-item ID and checks the queue table before any re-queue.
- **Menu MCP server** (stdio, spawned by the Codex daemon):
  - Codex calls it with its draft. The relay stores the draft and shows a form elicitation with a single-select of the four choices.
  - The form shows the full stored draft when it fits. Otherwise it shows the first lines, the size and the path of the exact draft file. Codex also prints the draft in chat for reading; the stored file is authoritative (Q21).
  - The tool timeout pauses while the form is open, and a form left 445 s was still answered normally [spike]. Closing the window leaves the form pending, and reopening shows it again [tested].
  - While the form is showing, typing in Codex is ignored. Discuss or Esc gives the keyboard back.
- **Notifications:** a plain Windows toast through built-in `powershell.exe` WinRT, with no module or clickable links (Q14). [unverified on the dev box]

### Codex side (dedicated reviewer `CODEX_HOME`, e.g. `~/.codex-reviewer`)
- It has its own `codex login` and its own daemon, so hooks in the user's own `~/.codex` don't fire here (Q28).
- `relay review <label>` opens `CODEX_HOME=… codex resume <thread-id>`. It must not pass `--profile` or `-c` overrides, because either one disconnects the window from the shared daemon and queued turns would no longer show in it [source/spike]. `-s` / `-a` are fine [spike].
- Threads are created explicitly with `-s read-only -a on-request`, and these settings stick to the thread.
  - `-a never` would silently decline every menu [spike].
  - Read-only writes fail without asking [tested].
  - Under `on-request`, Codex *can* ask to escalate a command. The persona says never to escalate, and the user declines any request that appears. Whether a granular approval policy removes this is [unverified].
- **Persona:** the user's own reviewer instructions, verbatim, in a file they keep outside the relay and map per project in `config.json` (Q18a); `persona/example.md` is a generic example. It goes in `developer_instructions` (injected when the thread is created [spike]), and a short persona header goes on every queued message [spike]. The persona is chosen per project, because the relay itself isn't specific to any project.
- **Relay operating instructions** (draft a return prompt, call the menu tool, never ask to escalate) live in a separate file, so the persona stays exactly as the user wrote it.
- A **Codex Stop hook** in the reviewer home tells the relay "turn ended". If no draft was saved, the user is notified: "assessment finished without a draft".

## Menu semantics

| Choice | Effect |
|---|---|
| **Send** | The relay hands the stored draft, byte-for-byte, to the waiting Claude Stop hook. |
| **Discuss** (also Esc) | The form closes. The user and Codex talk normally, then Codex saves a revised draft and the same menu appears again. |
| **Keep waiting** | The form closes and Claude stays held, up to the 4 h limit. Asking Codex for the menu shows it again. Leaving the form unanswered also keeps waiting. |
| **Finish without sending** | Claude is released and the handoff is closed. |

## Failure and edge rules (the user's rulings)

- **Failed:** the draft definitely wasn't delivered. Show the reason and offer Retry. There are no automatic retries.
- **Unknown:** the draft may have arrived. Check the Claude transcript for the draft ID before offering a resend, so Claude doesn't get the instruction twice.
- **4 h timeout:** keep the draft, and Codex shows "timed out, not sent" (Q17). An Esc release shows "released from Claude, not sent".
- **Send after Claude was released** (Esc or timeout): clipboard copy only (Q11b).
- **Claude session ended:**
  - Stop sending to it and keep the draft. Offer Copy only (Q23); resuming the session never sends it.
  - Tell Codex: *"The originating Claude session has ended. This draft has not been sent. Review any outstanding concerns and tell the user what still needs attention."*
- ~~Two Claude agents never work on the same ticket~~ (Q25, **reversed 2026-10-25**: the user's implement and verify sessions work the same ticket at once). There is still one Codex thread per ticket; it now holds hand-offs from several sessions. See *Cross-session delivery*.
- Several Claude sessions, on the same or different tickets, may wait at once. Each hand-off records its source session, and each draft is sent only to the recipient the user picks.

## Cross-session delivery (grilling from 2026-10-25)

Goal: stop copying instructions between Claude windows. A verifier's report reaches Codex. The user discusses it there, picks the recipient (often the implementer), and sends the final approved instruction straight to it.

### Terms
- **Source session:** the session whose report (hand-off) produced a draft. It stays the source even if other reports arrive later.
- **Recipient:** the session the user chooses to Send a draft to.
- **Held session:** a session waiting in its Stop hook for the user's decision (a linked session, or one forwarded once with `/relay-review`).
- **Ticket label:** the name a session is linked under; with the project, it identifies the ticket's one Codex conversation (Q17).
- **Report** (hand-off, `H<n>`): one finished Claude turn filed into the ticket's conversation.
- **Draft** (`D<n>`): Codex's proposed next instruction for Claude, sent byte-for-byte on Send.
- **Menu:** the form Codex shows for a draft (Send to … / Copy / Discuss / Keep waiting / Finish).
- **Role:** the optional session name given with `--as` (Q18).

### Settled before the session (the user)
- The user has the final say before anything reaches Claude, and nothing is delivered without their explicit Send.
- They pick the recipient before Send, and the destination is shown clearly.
- Only the approved final instruction goes to the recipient: never the Codex discussion, never their dialog answers, never conversations or tool histories.
- They answer Claude's question and permission dialogs themselves.
- After a send to a different session, the source session stays waiting.
- Each instruction is delivered exactly once.
- Persona and standing context are kept as they are (standing context: the main checkout's `.scratch/codex-handover.md`, tracked per thread by sha256, recorded after queueing; missing, unreadable or empty means no block; Codex only).
- Out of scope: Agent Teams, other machines, and redesigning the handover note.

### Rulings (2026-10-25)
- **One conversation per ticket, several sessions** (Q1b). The implement and verify sessions link to the same ticket, and the loop runs in one Codex conversation. Each hand-off keeps its source session. Each approved draft has an explicit recipient. The relay never infers either from whichever hand-off is newest.
- **Delivery route** (Q2). A held recipient gets the draft through its own waiting Stop hook. If it isn't held, Copy is offered. At Send, the relay checks that the recipient is still waiting. If it isn't, the relay says so plainly and the user chooses what happens next.
- **Menu** (Q3). The tested single-choice form, with one **Send to …** option per eligible recipient, each naming its recipient clearly, then Discuss / Keep waiting / Finish without sending. The source session is listed first. Codex may name a suggested recipient, shown as text only; it never changes the order or the selection.
- **Context** (Q4). Codex (in the hand-off header) and the user (in the menu) get folder, branch, HEAD and session title. The destination display shows when that information was captured, because it can change while a session waits. Claude gets only the exact approved draft.
- **Authority** (Q5). An approved draft is a valid work instruction ("fix it, test it and commit it" is a legitimate draft). Hook delivery doesn't satisfy a separate permission gate. Codex must not claim the user answered a dialog they haven't answered. Acceptance tests 3 and 3b test execution and permission handling separately.
- **Terms** (Q6) live in this section; no separate glossary.
- **Open menu and queued reports** (Q7). An open menu is a form inside a Codex tool call, so that turn stays open. Codex runs one turn per thread at a time, and a queued report waits until the turn ends. That's a Codex limitation combined with the menu-in-Codex choice (Q24). The report isn't lost: it is in the relay's store and Codex's queue, and its session stays held. Rulings:
  - Codex's queue stays the only queue.
  - The user is notified when a report is queued behind an open menu.
  - An open menu is never redrawn. A redraw resets the highlighted option, and a stray Enter could send to the wrong session.
  - Choosing Discuss lets the queued report run first; the relay tells the user so. There is no automatic "resume discussion" step: Codex keeps the context.
  - One acceptance test checks what happens after the menu tool returns with a report queued, in particular whether that report joins the same turn [tested 2026-10-31: it runs in a new turn; see Build findings].
- **Explicit source** (Q8). `propose_reply` and `show_menu` name the hand-off they answer (e.g. `H5`). The relay rejects a call that names none, one from another ticket, or a closed one, and tells Codex why. It never falls back to the newest hand-off. The menu shows the source first.
- **Eligible recipients** (Q9). Only sessions held on the same project and ticket. Two projects can both have a ticket `123`; the label alone never connects them.
- **Recipient unavailable at Send** (Q10). Nothing is sent, and the menu returns with the reason, offering Copy for that recipient, Send to any other held session, Discuss, Keep waiting and Finish. The existing delivery and uncertainty checks are reused. An uncertain delivery (`unknown`) is never shown as an ordinary "not sent", and no further Send of that draft is offered until the transcript check resolves it.
- **The recipient's previous draft** (Q11). Once a recipient receives an instruction, its own earlier unsent draft is closed ("not sent: the session received D<n> instead") and can't be sent later.
- **Finish** (Q12) releases only the draft's source session. The option names it, e.g. "Finish without sending — release verifier". No extra confirmation.
- **Notices to Codex** (Q13). After a send to another session: "Sent D8 to implementer (H4). Verifier H5 is still waiting; use show_menu H5 to send it something or finish it." The recipient's next hand-off header adds "Last instruction delivered: D8, source verifier H5." These go to Codex only.
- **Draft lifetime** (Q14): **deferred**. Today's expiry rules stay, applied to the draft's source hand-off. A draft is sendable only while its source is held. A superseded, released, timed-out or ended source leaves Copy only.
- **Destination freshness** (Q15): optional. Each destination shows when its folder, branch and HEAD were captured. Re-reading branch and HEAD when the menu opens goes in only if it's straightforward; it doesn't block the release.
- **No hold command** (Q16). Linked sessions are held after every turn already. A recipient that's busy, released or past its 4-hour hold gets Copy.
- **Conversation identity** (Q17). Today the label alone identifies a ticket's conversation (`threads.label` is the primary key), which breaks Q9. A conversation is now identified by project plus label. The project is the main checkout, the same key the persona lookup uses, so a worktree and its main checkout share one. Existing rows get their project from their stored folder. `relay review "<label>"` uses the project containing the current folder; failing that, the only match; with several matches it lists them and exits.
  - *Known limitation (build of #3):* the migration finds a project with `git` run in the stored folder. If that folder no longer exists, such as a deleted worktree, the conversation is keyed by that folder's own path instead of the main checkout. It is still found by `relay review` from anywhere when it is the only match for its label, but it is not shared with the project's other sessions, and the same label there starts a new conversation.
- **Session names** (Q18). `/relay-link` and `/relay-review` take an optional `--as <role>`, e.g. `--as verifier`. It's read before the rest is joined into the label, so labels with spaces still work. Without a role, the name is the folder name plus a short session ID. Two held sessions with the same role on one ticket get the ID appended. The name is used in Send to …, Finish — release …, toasts and hand-off headers.
- **Development and tests** (Q19). Build in a separate development checkout or worktree on the dev box, never the installed `~/tools/cc-codex-relay`. Acceptance tests run on the dev box with their own relay state folder, reviewer Codex home and throwaway repo, so the installed relay, `~/.codex-reviewer` and the user's projects stay untouched. A run is INVALID only if the installed store gained, lost or changed a row the run wrote: one naming a test session, test label or acceptance folder, or belonging to a hand-off or Codex thread that does. Message text doesn't count. The user's own sessions keep using the installed relay during a run, and their rows are listed as not attributed to it (#11). Modal isn't used.
- **Generic wording** (public-release adjustment). New code and instructions don't hardcode the user's name or a project. They say "the user", e.g. "never claim the user answered a permission dialog". Project paths, personas and personal notes stay in configuration. Confirmed 2026-10-25: the existing strings in code and Codex instructions that name the user become generic in this release too, including the standing-context block heading and the session-ended notice.

### Agreed scope (confirmed 2026-10-25)
1. **Store:** conversations keyed by project plus label, with existing rows migrated; an optional role on each link; folder, branch, HEAD and capture time on each hand-off; each draft records its source hand-off and its recipient.
2. **Exactly-once, keyed on the recipient:** the recipient's waiting hook claims only drafts addressed to it. The `sending → sent` step, the transcript check (the recipient's transcript), stale-hook detection and the at-Send liveness check reuse today's code. `unknown` offers no Send.
3. **Menu:**
   - one Send to <name> per held session on the same project and ticket, source first, then Discuss / Keep waiting / Finish — release <source>;
   - the text shows the source, each destination's folder, branch and HEAD with capture time, Codex's suggestion as text only, and reports still waiting;
   - it's withdrawn as today when the source stops waiting, leaving Copy only;
   - a recipient that stops waiting causes no redraw; Send catches it and shows the Q10 menu;
   - re-reading branch and HEAD only if simple.
4. **Tools:** `propose_reply` and `show_menu` need `H<n>`, with the Q8 rejections; the Q13 notices; a recipient's earlier unsent draft is closed once it receives an instruction.
5. **Notices:** a toast when a report is queued behind an open menu; toasts and the session-ended notice name the session; the no-draft notice stays per hand-off.
6. **Codex instructions,** in generic wording: always name the hand-off; recipient suggestions are optional and shown as text; never claim the user answered a dialog; Discuss lets a queued report run first.
6b. **Generic wording everywhere in code and instructions:** existing strings that used the user's name become "the user" (`ops_instructions.md`, menu text, tool results, notices, the standing-context heading, the session-ended notice). Persona content and project paths stay in configuration.
7. **Unchanged:** persona, standing context, the thread-loaded guard, the 4-hour hold, the stop-hook cap, today's expiry rules.
8. **Docs:** this record; `GUIDE.md` and `README.md` drop "one Claude session per ticket" and document `--as`, Send to … and Copy; `install-claude` refreshes its managed command files.
9. **Acceptance tests (dev box, isolated):**
   - same ticket, two sessions: each Send reaches only its recipient, and the source stays held;
   - 3 (ordinary execution) and 3b (permission handling) separately;
   - a recipient released before Send gets the Q10 menu;
   - an interrupted send ends as `unknown` with no Send offered;
   - a report queued behind an open menu isn't lost and the toast shows, plus the single after-menu-return test;
   - a missing or wrong `H<n>` is rejected;
   - the same label in two projects gives two conversations;
   - long Send labels show legibly;
   - the first hand-off in a new conversation carries standing context (its first live check), and persona still applies;
   - the existing tests are rerun.

Out of scope: native-socket delivery, a hold command, the wider draft lifetime rule (Q14), Agent Teams, other machines, and handover-note changes.

## Build findings (2026-10-20, Modal)

These came out of the first build and test runs. Each one is reflected in the code. Run IDs such as `results/20261031-234151` name raw evidence folders that aren't published; [docs/acceptance-results.md](docs/acceptance-results.md) summarises the results.

- **Modal runtime.** Codex's read-only sandbox (bubblewrap) can't start under Modal's default gVisor runtime (`bwrap: loopback: Failed RTM_NEWADDR`). The dev sandbox therefore uses `runtime="vm"`. On the dev box this doesn't arise.
- **Daemon start after reboot.** Codex keeps its control socket in `/tmp` behind a link in `CODEX_HOME`. After a WSL reboot or a new sandbox, the link is dead and the daemon refuses to start ("File exists"). The relay removes a dead link before starting the daemon.
- **Tool-server start-up.** Codex drops an MCP server that takes more than 10 s to start, for that thread. A cold Python start once took about 11 s, and Codex then reported `propose_reply` as unavailable. The relay's server now gets `startup_timeout_sec = 60`. If a draft still never arrives, the no-draft notice catches it.
- **Menu withdrawn when Claude stops waiting.** An open menu blocks queued messages to that thread, so a "session ended" notice would wait until the user answered the menu. While a menu offering Send is open, the relay watches the handoff. On Esc, exit, timeout or session end it withdraws the form (MCP `notifications/cancelled`; Codex clears the form cleanly), and the session-ended notice is delivered straight away.
- **After Discuss or Keep waiting, Codex is told to end its turn.** Otherwise a message the user types while Codex is still writing is held as "submitted after next tool call" and can sit there.
- **Claude's wording.** Claude Code shows delivered drafts as `Stop hook error: <draft>`. That's its fixed label for Stop-hook feedback; Claude still treats the text as the user's instruction.
- **Graceful daemon restart waits for an open menu.** `codex app-server daemon restart` doesn't complete while a review menu is open (it timed out after 45 s with the same pid). An automatic Codex updater may wait or skip while a menu is open. If the daemon is killed outright, reopening the conversation showed the menu again and Send delivered (observed in test 8; the mechanism isn't verified).
- **Claude Code's consecutive Stop-hook cap.** Claude Code ends a turn after 8 consecutive Stop-hook blocks and ignores the next one ("blocked the turn from ending 9 consecutive times — overriding"). Every Send in the review cycle is one block, so the 9th Send in a row was shown to Claude but not acted on. `install-claude` sets `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000` in `~/.claude/settings.json` `env`; 10 consecutive Sends then all landed (check E2).
- **Claude killed outright.** A hard-killed Claude (terminal closed) leaves the hook running. The hook now also watches Claude's pid and releases the handoff, and the open menu is withdrawn (check E1).
- **Model reliability.** At low effort, gpt-6-luna sometimes skipped the relay tool calls it was told to make: no draft, or no menu when asked. The relay surfaces this ("assessment finished without a draft"), but it means more nudging. Tests use gpt-6.1-sol at low effort. For real use, pick a model of at least that level.

## Build findings (2026-10-27, dev box)

- **Acceptance on the dev box.** Tests 1–11, E1 and E2 pass in the isolated environment (`results/20261026-202157` for test 1, `20261027-030548` for 2–7, `20261027-031204` for 8–13), each run with the installed store unchanged. Design test 12 (the toast and the real VS Code terminal) needs a person and is left for the real-session acceptance (#8).
- **Codex rate limits stall an assessment.** A rerun of tests 2 and 3 stalled at `assessing`. The development Codex log showed HTTP 429 with back-offs of 494 s and 251 s, all ending at the same moment, so it was one account-wide window. Codex retries by itself, and the relay was not at fault. The harness now extends a wait to the end of a logged back-off. The same delay can occur in real use; the no-draft notice doesn't fire because Codex's turn is still running.
- **A draining daemon refuses new conversations.** While a graceful stop or restart waits on an open menu, the daemon still answers the relay's liveness probe but rejects `thread/start` ("Server is draining"), so a first hand-off in that window fails and releases Claude. A `/relay-link` or `/relay-review` in that window records the link and says the conversation couldn't be created yet (#9). The harness kills its development daemon if a graceful stop takes over 15 s. The relay itself is unchanged; this is a known limitation.
- **Upgrading while sessions are held** (review of #6). A held session's hook keeps running the code it started with, for up to 4 hours, and so does the reviewer's menu server. Hooks from before recipients collect drafts by the report they answer. Hand-offs now record that their hook collects by recipient: a report from an older hook can only be sent to its own session, and an older-hook session is never offered as another draft's recipient. A draft approved by an older menu server has no recipient and goes to its own report's session.
- **Conversation key at Stop** (review of the #4 build). The conversation, persona and standing context come from the folder the session was linked in. The folder, branch and HEAD in the hand-off header come from where the session is when its Stop hook fires.

## Build findings (2026-10-31, real-session acceptance, #8)

- **Acceptance (#8).** On the final code (with the queue fix below), tests 1–11, E1, E2 and the cross-session checks 14–18 all pass in `results/20261031-234151`, with the installed store unchanged. The by-hand looks (tests 17 and 18, and the toast in 14) are in the earlier runs listed next; the fix doesn't touch what was seen. Earlier runs that day, kept as evidence: `20261031-222316` (stopped: Codex's update screen, below; it never reached its end-of-run check, so the installed store was searched afterwards, read-only, for any row naming an acceptance path, that run's tag or a test session: none), `20261031-224316` (1–13 pass, before the queue fix), `20261031-225719` (15–18 pass; the by-hand looks for 17 and 18), `20261031-231020` and `20261031-231758` (14, before and after the queue fix; 15 again). Design test 12's cycle: `20261031-233740-manual`. After the code review's test-harness fixes, tests 14 and 16–18 pass again in `20261101-000622`.
- **Two sessions on one ticket** [tested] (extended test 2, test 15). The verifier's menu lists Send to verifier first, then Send to implementer, and Finish names the verifier. Its draft sent to the implementer reached only the implementer; the verifier stayed held. The implementer's next hand-off header said "Last instruction delivered: D3, source verifier H2". A later Send to the verifier reached only the verifier.
- **A report queued behind an open menu** [tested] (test 14). It waits in Codex's queue with its session held, and the relay raises the queued-behind-the-menu notice. **After the menu tool returns (Discuss), the queued report runs in a new Codex turn; it doesn't join the menu's turn.** That settles the open question under Q7.
- **Queue item never recorded (fixed).** Codex answers `thread/queue/add` with `{"queuedSubmission": {"id": …}}`; the relay read only `item` or `submission`, so every hand-off stored no queue item. A queued report was never recognised as queued: the menu's "Reports waiting for assessment" line and the Discuss notice that a queued report runs first never appeared, and the no-draft check couldn't see that a report was still queued. Reports weren't lost. The unit fakes replaced `queue_message` whole, so no test read Codex's real reply; `tests/test_codexrpc.py` now uses the shape from Codex's protocol schema (`codex app-server generate-json-schema --experimental`). Upgrade-safe: a hook started before the fix still stores no queue item, and for it both checks behave exactly as before (no item is never found in the queue).
- **Standing context, first live check** [tested] (test 16). The first hand-off of a new conversation carried the standing-context block, from the main checkout and from a worktree of it; both conversations were keyed to the main checkout and had the test persona in their instructions. Codex acted on it: both drafts began with the marker the standing context asked for.
- **Permission handling** [tested] (test 3b, test 17). A session in default permission mode was sent "run `touch …`". Claude raised its own "Do you want to proceed?" prompt; 20 s later, and again after a person looked, it was still unanswered and the file didn't exist. The relay's hook had already exited, so nothing of the relay could answer it.
- **Long Send to … labels** [tested] (test 18). Roles of 40-odd characters show in full at 220 and at 80 columns; at 80, only "Finish without sending — release …" wraps. By hand: both Send options readable in full and clearly distinguishable; the wrapped Finish still readable.
- **Cycle from the real VS Code terminal** [tested] (design test 12). Claude in a VS Code terminal with the test configuration, Codex in a second tab: Send at 23:39:53, delivered within a second, Claude's next report assessed in the open window 11 s after Send, with nothing typed; then Finish. Installed store unchanged.
- **Toasts not shown on the dev box** (design test 12, toast half: **not met**). The relay raises its toasts under Windows PowerShell's sender ID (`{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`). On this machine the notification platform has toasts off for that sender (`s:toast = 0` in `wpndatabase.db`, registered 2026-10-21; 4 of 426 senders are off, Explorer and Windows Terminal are on), and the notifier reports `DisabledForApplication`. PowerShell still exits 0 with no error, so the relay records the notice as given. Settings → Notifications has no Windows PowerShell entry. Setting the mirror value `HKCU\…\Notifications\Settings\<sender>\Enabled` to 1 and restarting the user's `WpnUserService` changed nothing; that registry value was then set back to 0, as it was. Open: re-enabling that sender, a different sender ID, or the relay reporting a disabled notifier.
- **Codex's update screen covers the window.** When a new Codex release came out mid-run, every Codex window opened on "Update available", and tests timed out waiting for the menu. The development Codex home now sets `check_for_update_on_startup = false`.
- **Not re-run live:** a recipient that stopped waiting before Send (Q10), a missing or wrong `H<n>` (Q8) and the same label in two projects (Q17) are covered by unit tests (`test_recipients.py`, `test_report_identity.py`, `test_conversation_identity.py`), not by #8's real-session run.

## Build findings (2026-11-01, #9 live check)

Run against `a5ad321` in the isolated environment, with the development Codex daemon stopped before each test; then against the fixes below. The installed store was unchanged in every run (each run's own check; the runs a time limit cut short were checked afterwards, read-only, for any row naming one of that evening's 20 test sessions, a T19/T20 label or an acceptance path: none).

- **Conversation at link time** [tested] (tests 19, 20; `results/20261101-230815` … `20261101-232421`). With the daemon stopped, `/relay-link` and `/relay-review` start it and create the conversation inside the command in 0.4–0.8 s (link event to `thread:created`); Claude's reply appears 2.7–8.7 s after typing; `relay review` opens the conversation 1.0 s after launch, before any hand-off. Creation queues nothing: no queue item, no turn and Codex idle, also 15 s after opening. The forwarded reply of `/relay-review` lands in the same conversation 2–3 s after creation. Every `relay review` that opened the conversation in these tests was the development executable, confirmed against `16ce013`: the harness execs the worktree's `bin/relay` by absolute path (`codex_open`, never the `relay` on PATH, which runs the installed checkout), and `bin/relay` loads `ccrelay` from its own checkout. The installed executable ran once, when Claude ran `relay review` itself in `20261101-231128` (it failed; see below), never as an opening check.
- **`/relay-review` couldn't find the previous reply under `CLAUDE_CONFIG_DIR` (fixed).** It read the transcript from `~/.claude/projects` only; Claude Code keeps it under `CLAUDE_CONFIG_DIR` when that is set, as in the acceptance environment. `cli.transcript_path` now reads it from there.
- **The command file's echo (fixed).** Claude is told to echo the relay's output, and Claude Code doesn't show the `!` output itself. With the instruction after the output, Haiku answered with only the label or a paraphrase (also seen with the old wording in earlier runs), dropping the `relay review` hint; it also copied a trailing instruction or the marker. With the hint as a bare "open it now: relay review …", Claude in auto mode **ran `relay review` itself** with its Bash tool (it failed: Codex needs a terminal). The command file now has the marker, then the instruction (copy only the text inside `<relay-reply>`; don't run any command it names, that is the user's, in their own terminal), then the output inside `<relay-reply>`; the hint is in the reply's first sentence and says "from your own terminal". On that file, 4 of 4 completed runs showed the output exactly and ran no command, as did 3 more runs whose verdict was cut short by a time limit (their transcripts were read afterwards). A command file installed before this keeps the old wording until `install-claude` runs again.

## Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Keep ChatGPT web | No documented way to write into it programmatically. |
| Claude inbox socket for the return | Claude frames the message as a peer's, "not typed by your user". Any process running as the same user can post to it without auth [tested]. Re-checked 2026-10-25 against the cross-session messaging docs, still rejected for delivery: the recipient is told it came from another session; it can't approve anything or change settings or `CLAUDE.md`; slash commands in it don't run; a bypass-mode recipient holds it in a 5-minute dialog and then drops it; the user sees only a one-line preview; identical repeats are dropped; the sender gets no delivery receipt [source]. |
| Separate labels (and Codex conversations) for implement and verify | The user wants the implement → verify → fix loop in one conversation per ticket (2026-10-25). |
| Context header prepended to the delivered instruction | It breaks byte-for-byte delivery and adds text the user didn't approve. The context goes to Codex and the menu instead. |
| VS Code URI prefill | The user runs the CLI, not the extension; all transcripts show `entrypoint: cli`. |
| `codex exec resume` for assessment | See above: writer lock, pulls queued items, resets the sandbox. |
| Menu in a relay terminal only | The user ruled the menu belongs inside Codex (Q24). |
| Fork uzi0espil/Codex-Claude-Reviewer-Bridge | Publishing goes through the Codex model, there's no revise loop, the Codex WebSocket is unauthenticated, Playwright is on with unrestricted file access, and next-prompt auto-delivery breaks the release rules. It would mean maintaining a fork of about 13k lines. |
| openai/codex-plugin-cc | Its review gate sends findings back to Claude without a human step, and nothing has been merged since July. |

## Environments

- **Build and test:** a Modal sandbox holding the whole stack (temporary Claude sessions, Codex daemon and window, relay, throwaway repo). The trial driver and fixtures have since been removed.
  - Modal resources: secret `claude-oauth`, volume `codex-reviewer-home` (the sandbox's Codex login), app `cc-codex-relay-trial`.
  - Sign-in: Claude Code with a token from `claude setup-token`, and Codex with a device sign-in (`codex login`) inside the sandbox (Q31a, Q32a).
  - Trial 2026-10-20 [tested]:
    - gVisor runtime, non-root user, tmux, background processes and Unix sockets all work;
    - Claude hooks work with the token;
    - Codex daemon, queue, live assessment, background assessment with the menu pending, and the menu shown again on reopen all work;
    - Send and Discuss reach the menu tool, and Esc releases Claude.
- **Use:** the dev box (WSL2). Claude runs in the VS Code terminal, Codex runs in a terminal tab, and the relay runs under `~/tools`. The Windows toast and the VS Code terminal can only be checked here.

## Acceptance tests (gate before use)

These run in throwaway sessions against a throwaway repo, never a real project.

1. **Exact text.** Send a draft with a unique ID, several lines, quotes, backticks, non-ASCII characters and about 4 KB of text. It must match the Claude transcript byte-for-byte after the prefix. [passed in trial for a short draft; the 4 KB case is still to run]
2. **Correct session.** Two linked sessions on different tickets. Each Send reaches only its own session. *Extended 2026-10-25:* two sessions on the **same** ticket; a Send reaches only the chosen recipient, and the source session stays waiting. [passed 2026-10-31, #8]
3. **Ordinary execution** (split 2026-10-25). An ordinary instruction (e.g. fix, test, commit) is actually carried out. Claude only asking a question about it is a **fail**. [passed in trial under the old wording]
3b. **Permission handling.** An instruction needing a permission the session lacks still raises Claude's prompt for the user; delivery never answers it. [passed 2026-10-31, #8, with a person watching the prompt]
4. **No duplicates.** Double Send, and killing the relay between `sending` and `sent`, give at most one delivery. `unknown` is resolved by checking the transcript.
5. **Visible failure.** A killed hook gives `failed` with a reason. A changed hook format gives `failed`. An ended session gives the ended notice to Codex.
6. **Esc** releases the wait cleanly. [passed in trial; the relay must still detect it and show "released from Claude, not sent"]
7. **Open-window cycle.** The user's normal cycle (above) with no exiting and no typing. [the core mechanism passed in trial]
8. **Menu survival.** A pending menu survives a daemon restart, including an hourly updater, and a long idle unload. [unverified]
9. **Escalation prompts** under `on-request`, and whether a granular policy removes them. [unverified]
10. **Thread-loaded guard.** No path lets opening a thread fire an assessment.
11. **No-draft detector** fires when Codex ends a turn without saving a draft.
12. **Dev-box checks:** the toast appears, and the cycle works from the real VS Code terminal. [cycle passed 2026-10-31; toast **not met**: Windows has toasts off for the PowerShell sender on the dev box, see Build findings 2026-10-31]

## Evidence

- Integration research notes, 2026-10-19 (not published).
- Spikes on the dev box, 2026-10-19/20:
  - Claude return paths (inbox socket against Stop-hook feedback);
  - `codex exec resume` behaviour (lock, flags, exit codes);
  - `codex queue`, the daemon, MCP elicitation and the persona, using an isolated `CODEX_HOME`.
- Modal trial, 2026-10-20 (driver and fixtures since removed).
