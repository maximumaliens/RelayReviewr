# cc-codex-relay

The relay passes each reply from a Claude Code session to a Codex conversation for review. Codex checks the reply and drafts your next instruction to Claude. You read the draft, talk it over with Codex if you like, and choose **Send** to give it to Claude. It replaces copying text between Claude and a reviewer by hand.

- **Nothing reaches Claude unless you choose Send.** Codex can't send anything by itself.
- **It never answers Claude's questions or permission prompts.** You answer those yourself, in Claude.
- Claude receives only the draft you approved, exactly as shown: never the discussion, the menu or any extra text.
- It only acts on Claude sessions you have linked. Every other session is unaffected.

The relay is a small Python program (standard library only). The design notes and the reasons behind each rule are in [DESIGN.md](DESIGN.md). Development and testing are in [docs/development.md](docs/development.md).

## Words used here

- **Ticket label:** a name you give the work, such as `issue-42`. Each label has its own Codex **conversation**, so Codex remembers earlier reviews of the same ticket.
- **Linked session:** a Claude session you've attached to a ticket label with `/relay-link`. Each reply it finishes is sent for review.
- **Report** (also called a hand-off, numbered `H1`, `H2` …): one finished Claude reply, sent to the ticket's Codex conversation.
- **Held session:** a session that has finished a reply and is waiting for your decision (a linked session, or one you used `/relay-review` in). Claude shows *"running Stop hooks…"* while it's held.
- **Draft** (numbered `D1`, `D2` …): the next instruction Codex suggests for Claude.
- **Menu:** the list of choices Codex shows you for a draft: Send, Copy, Discuss, Keep waiting or Finish.
- **Source session:** the session whose report a draft answers.
- **Recipient:** the session you choose to Send a draft to. It's usually the source session, but it can be another session on the same ticket.
- **Role:** an optional name for a session, such as `implementer` or `verifier`, set with `--as`.

Three technical terms, in one line each:
- A **Stop hook** is a program Claude Code runs when it finishes a reply. The relay's Stop hook passes the reply on and holds Claude until you decide.
- An **MCP tool server** is a small program Codex can call. The relay's one lets Codex save its draft and show you the menu.
- A **worktree** is a second folder for the same git repository. A worktree shares its project's conversations, persona and standing context.

## Requirements

| Item | Requirement |
|---|---|
| System | Linux. Tested on Windows with WSL2, which is what notifications and Copy need (below). |
| Python | 3.11 or newer at `/usr/bin/python3`. No extra packages. |
| Claude Code | The command-line program (CLI), signed in (run `claude`, then `/login`, if you haven't already). Tested in a VS Code terminal; not tested in the VS Code extension's panel. |
| Codex CLI | Installed and signed in for a separate reviewer home (step 3 below). |
| Other | `git`, and internet access for Claude and Codex. |
| Windows tools (optional) | `powershell.exe` for notifications and `clip.exe` for Copy. Both come with Windows and are found from WSL. Without them, notifications only go to a log file and Copy tells you where the draft file is. |

The relay uses no API keys. Claude and Codex run on whatever sign-in they already have.

## Install

1. **Get the code** and put its `bin/` folder on your `PATH`, for example:
   `git clone <this repository> ~/tools/cc-codex-relay` and add `export PATH="$HOME/tools/cc-codex-relay/bin:$PATH"` to `~/.bashrc`.
2. **Write the relay's settings** in `~/.local/state/cc-codex-relay/config.json`. List each project you'll review, with its persona file (see *Personas and standing context*):
   ```json
   {"codex_home": "/home/you/.codex-reviewer",
    "personas": {"/home/you/my-project": "/home/you/reviewer-persona.md"}}
   ```
   Start your persona file from a copy of `persona/example.md`. Use the project's main folder; its worktrees are found from it. Use `""` as the persona to review a project without one.
3. **Sign in the reviewer Codex** (only you can do this): `CODEX_HOME=~/.codex-reviewer codex login`. This is a separate Codex home: your normal `~/.codex` isn't touched. To choose the reviewer's model and reasoning effort, set `model` and `model_reasoning_effort` in `~/.codex-reviewer/config.toml`.
4. **Set up the reviewer Codex:** `relay install-codex`. It adds the relay's tool server, keeps conversations loaded so opening one never starts a review by itself, and marks your listed projects as trusted. Your own settings in that file are kept. Run it again after changing the project list.
5. **Set up Claude:** `relay install-claude`. It adds the relay's Stop and SessionEnd hooks to `~/.claude/settings.json` (with a 4-hour wait limit), raises Claude Code's limit on repeated Stop-hook replies so a long review loop isn't cut off, and adds the `/relay-link`, `/relay-unlink` and `/relay-review` commands. It backs up `settings.json` first (`settings.json.relay-backup-<date-time>`). It refuses, changing nothing, if a `relay-*.md` command file it didn't write already exists.
6. **Start new Claude sessions** after installing. A session that was already open may not have loaded the hooks.

### Uninstall

1. `relay uninstall-claude` removes only what `install-claude` added. It backs up `settings.json` first, and leaves a setting alone if you changed it after installing.
2. `CODEX_HOME=~/.codex-reviewer codex app-server daemon stop` stops the reviewer's background service.
3. If you want, delete the code folder, `~/.local/state/cc-codex-relay` (settings, review history and every draft) and `~/.codex-reviewer`.

## Everyday use

1. **Start Claude** in a project as usual: `cd ~/my-project && claude`.
2. **Link the session** by typing `/relay-link issue-42` in Claude. From now on, every reply in this session is reviewed. With no label, the git branch name is used.
   - Linking also creates the ticket's Codex conversation (or finds the existing one), so you can open it straight away. It stays empty until the first report arrives. If Codex can't be reached, the link still works: Claude's reply says so, and the conversation is created when the first reply is sent.
   - **Name the session's role** with `--as`: `/relay-link issue-42 --as verifier` (or `/relay-link --as verifier issue-42`). Codex's report header then says who sent it, with the folder, branch and commit the session was on. Without `--as`, the session is named by its folder plus a short session ID. Only Codex and the menu see this name; Claude receives nothing extra.
3. **Work as normal.** When Claude finishes a reply, it shows *"running Stop hooks…"*: it's held, waiting for your review. Don't press Esc there unless you want to release it.
4. **Open the Codex conversation** in a second terminal, at the Linux prompt (not inside Claude):
   `relay review "issue-42"`
   - You can open it any time after linking: before the first report, or when the notification "issue-42: verifier report H7: review ready" appears.
   - Use the label exactly as you linked it, in quotes if it has a space.
   - Leave the window open and later reviews for the ticket appear in it by themselves. Close it with Ctrl+C; running the command again reopens the same conversation.
   - A conversation belongs to a project plus a label, so two projects can both use `issue-42` without sharing one. `relay review` opens the one for the project you run it from. From any other folder, it opens the only match; if several projects have that label, it lists them and opens none.
5. **Read Codex's assessment and draft, then choose from the menu** (below).
6. **Stop reviewing** a session with `/relay-unlink` in Claude.

To review just one reply, type `/relay-review issue-42` (it also takes `--as`). The last reply Claude finished is sent once, and the session is held like a linked one until you decide.

- **What it sends:** Claude's last reply, not counting replies to the relay's own commands. After `/relay-link`, for example, it sends the reply from before you linked, not the "Linked this session…" line. If the session has no reply of Claude's own yet, the command says so and sends nothing.
- **It doesn't change an existing link.** A session that isn't linked stays unlinked afterwards. A linked session stays linked, with its label and role, and its later replies are reviewed as before.
- **In a linked session,** leave out the label and `--as` to send the reply to the session's own ticket under its own role. If you name a different label or role, only that one reply goes there, and Claude's reply says the session stays linked to its own ticket. Whatever Claude does next is reported to its own ticket.
- **Background tasks:** if the `/relay-review` turn ends while Claude still has background tasks running, or you interrupt it, the reply is sent when the session's next turn ends without them. In a linked session it's sent in place of that turn's own reply.

### The menu

| Choice | What it does | When to use it |
|---|---|---|
| **Send to <session>** | That session receives the draft exactly as shown and carries on. When it next finishes, its new report appears in the same conversation. | The draft is right. |
| **Copy for <session>** | Puts the draft on the clipboard for a session that isn't held (busy, released or past the 4-hour limit). Nothing is sent; paste it yourself. | The session you want can't receive right now. |
| **Discuss** (or Esc) | The menu closes and you talk with Codex normally. When Codex has a revised draft, the menu comes back. **Claude stays held** the whole time, so anything you type in Claude queues until Send, Finish or Esc. To answer Claude directly, choose **Finish** first (Codex will tell you, and can bring the menu back). | You want changes, or have questions. |
| **Keep waiting** | The menu closes and Claude stays held. Ask Codex for "the menu" when you're ready. | You need a break or more time. |
| **Finish without sending — release <session>** | Releases the source session with no reply. Other held sessions stay held. | Claude needs nothing more: for example, it only reported that it's done. |

**When Claude needs nothing.** If a report only restates a decision request, acknowledges, or waits for you, Codex doesn't draft an acknowledgement (any reply would come back as another report). It says so and shows a **no-reply menu** with Discuss, Keep waiting and Finish without sending, never Send or Copy, and recommends Finish. Nothing is sent or finished until you choose. If Codex later drafts a reply for the same report, the normal menu returns. You get the usual "review ready" notification, worded "no reply needed".

There's one Send option for each held session on the same project and ticket, with the source session first. The menu shows each one's folder, branch and commit, and when those were read. Codex may suggest a recipient; that's shown as text only and never changes which option is highlighted.

If the source session is no longer held (you pressed Esc, it timed out, or it ended), Send isn't offered: you get **Copy to clipboard**, Discuss and Finish. After you've sent a draft to a different session, its menu offers Discuss, Keep waiting and Finish for the source session, which is still held.

### Two or more sessions on one ticket

Link each session to the same label with its own role, for example an implementer and a verifier:
- in the first session: `/relay-link issue-42 --as implementer`
- in the second session: `/relay-link issue-42 --as verifier`

Both report into the ticket's one conversation, so Codex sees the whole loop. When you Send, you choose who receives the draft. For example, send the verifier's findings straight to the implementer. If you send to a different session, the source session stays held, so you can send it something later or finish it.

Codex handles one thing at a time per conversation. A report that arrives while a menu is open waits behind it, and you get a notification such as "issue-42: verifier report H7 queued behind the open menu". The open menu is never redrawn, so a stray Enter can't send to the wrong session. The report isn't lost: it's assessed as soon as you answer the menu. Choosing **Discuss** lets the queued report go first, and you carry on the discussion afterwards. The menu also lists reports still waiting to be assessed.

### Checking what's going on

`relay status` lists the last 20 reports: the ticket, the report's state, the session that sent it, and each draft's state and recipient. There's no command yet that lists linked sessions; `relay status` shows each session that has sent a report.

## What decides whether a reply is reviewed, or a session can receive

- **Only finished replies are sent.** A reply is sent for review when Claude finishes its turn. While Claude is asking you a question or waiting at a permission prompt, its turn hasn't finished: nothing is sent, and the session isn't held, so it can't receive a Send (Copy is offered instead). Answer Claude, and the reply it finishes next is sent.
- **Background tasks:** if a turn ends while Claude still has background tasks running, that turn isn't sent. The next turn that ends without them is.
- **Subagents:** replies from Claude's subagents are never sent; only the main session's reply is.
- **A newer reply replaces an older one:** if a session sends a new report before you've decided on the last one, the older one is closed (superseded). Its draft can only be copied.
- **The 4-hour limit:** a held session is released after 4 hours without a decision. The draft is kept, marked "timed out, not sent", and you can still copy it.
- **Releasing Claude yourself:** pressing Esc in Claude, or closing its terminal, releases it. If that session is the source of an open menu, the menu is withdrawn and the draft isn't sent.
- **Once a session receives an instruction,** any earlier draft written for its own reports is closed ("not sent: the session received D<n> instead").
- **If a session ends** with a draft unsent, Codex is told and lists what still needs attention.

## Skills and workflows

The relay works with any Claude workflow or skill. It doesn't know about them: it only sees finished replies and delivers approved drafts.

A draft reaches Claude as an instruction from a hook, not as something you typed. Skills that only you can start (for example, ones marked `disable-model-invocation`) must be typed by you in Claude; a relayed draft can't start them. A draft can ask Claude to use skills Claude may start by itself.

Claude Code shows a delivered draft as **"Stop hook error: …"**. That's Claude Code's fixed label for this kind of input. It isn't an error, and Claude still treats the text as your instruction.

## Personas and standing context

- A **persona** is your instructions for the reviewer: its role, tone and house rules. It's a text file. You set one per project in `config.json` under `personas`, keyed by the project's main folder. A generic example is in [`persona/example.md`](persona/example.md); copy it somewhere of your own and edit it.
- **Standing context** is a note about the project's current state that Codex should know, kept in the project's main folder as `.scratch/codex-handover.md`. The first report in a new conversation carries it, and it's sent again whenever the file changes. If the file is missing or empty, nothing is sent. Only Codex sees it.
- A conversation keeps the persona it was created with. Changing a persona file affects new conversations only; for an existing ticket, use a new label to start fresh.
- The relay also gives Codex its own operating instructions (draft a complete instruction or have the relay copy a stored report, keep each delivered instruction standalone because sessions are isolated, call the menu, stay read-only, never claim you answered a prompt). They're kept separate from your persona.
- Codex reviews read-only: it can read your project but can't change it.

### After you update the relay: what an existing conversation picks up

A new version adds a tool (`no_reply_menu`) and revises tool descriptions, which live in the tool server. Reopening the conversation in a Codex window (`relay review <ticket>`, then wait about 15 s for it to attach) or restarting the Codex daemon replaced the tool server in testing; in acceptance test 23 an existing conversation then listed the new tool, real Codex called it when asked, and a Discuss choice returned the revised guidance (one run, one Codex version; see DESIGN.md). Whether Codex chooses it unprompted wasn't tested. The guidance in the operating instructions about the no-reply path and Discuss keeping Claude held reaches only conversations created after the update; the short line in each report's header carries the no-reply hint to older ones. Update the relay when no session is held. The procedure doesn't refresh creation-time operating instructions.

This is what acceptance test 22 observed, with a conversation created from the previous version's instructions. It is a refresh investigation. It does not meet the requirement to update an existing conversation's operating instructions, which is still open.

- **Tool descriptions and the `propose_reply` schema.** Codex starts the relay's tool server and keeps it. In the test, a next turn left the same server process in place. Reopening the conversation in a Codex window (`relay review <ticket>`) replaced the server process when the test waited 15 s for the window to attach, and so did restarting the Codex daemon. Codex's own answers about its tools then matched the new server (served or hidden). Those answers are model-reported; the process IDs are what was observed. In an earlier run, a probe made immediately after the window opened still showed the old tool list, so give the window time to attach. This was seen on one Codex version, in a handful of runs.
- **Operating instructions.** They are given to Codex only when the conversation is created. The conversation's recorded developer messages never gained the new section after a next turn, a reopened window or a daemon restart. There is no tested way to update them for an existing conversation. This is **unresolved**: the relay doesn't pass instructions on resume. A new ticket label starts a conversation that gets the current instructions.
- **The short guidance in each report's header.** The relay's hook writes the header when it files a report, and it now says: give either a draft or a copy, never both, and the delivered text must stand alone. The wording is checked in unit tests. Whether it changes what an existing conversation does, without the full instructions, was not tested.

## Notifications

The relay notifies you when a review is ready, when a report is queued behind an open menu, and when something went wrong. Every notification names the ticket, session and report, for example "issue-42: verifier report H7: review ready".

- On WSL, notifications are Windows toasts sent through PowerShell. Windows can switch toasts off for PowerShell without telling the relay, so a missing toast doesn't mean nothing happened.
- Every notification is also written to `~/.local/state/cc-codex-relay/notifications.log`.
- The Codex window is the reliable signal: if it's open, the menu appears there when a review is ready.

## Troubleshooting

| You see | What to do |
|---|---|
| No notification | Check Windows notifications for PowerShell and Do Not Disturb. Run `relay status` or open the Codex conversation to see whether a review is ready anyway. |
| Codex finished without showing a menu | Ask Codex for "the menu". |
| "assessment finished without a draft" | Open the conversation and ask Codex to draft a reply. |
| "Assessment failed" | Codex couldn't start or confirm the review, and Claude was released. Check the reviewer Codex is signed in, then try again (for example with `/relay-review`). |
| The menu says "Not sent: <session> stopped waiting …" | Nothing was sent. Use Copy for that session, Send to another held session, or Discuss. |
| The menu says the draft "might have reached Claude" | A send was interrupted. Check that Claude session before doing anything else; no Send of that draft is offered, so it can't arrive twice. |
| "Claude hook incompatible" | Claude Code changed what it gives the Stop hook, and nothing was sent. Check for a relay update. |
| `relay review` says no conversation, or lists several projects | Check the label's spelling, or run it from inside the project's folder. |
| Codex asks permission to run a command | Decline it. Codex reviews read-only and is told never to ask. |
| Anything else | Collect the output of `relay status` and what both terminals show. |

## Files and commands

| Location | Contents |
|---|---|
| The code folder (e.g. `~/tools/cc-codex-relay`) | The relay. `bin/relay` is the program that you, Claude's hooks and Codex all run. |
| `~/.local/state/cc-codex-relay` | `config.json` (settings and the project → persona map), `relay.db` (review history), `drafts/` (every draft, exactly as written), `notifications.log`. Set `RELAY_HOME` to use another folder. |
| `~/.codex-reviewer` | The reviewer Codex: its own sign-in, settings and conversations. |
| `~/.claude/settings.json`, `~/.claude/commands` | The relay's two hooks, the Stop-hook limit setting, and the three `/relay-…` command files. |

| Command | Does |
|---|---|
| `relay review "<label>"` | Open the ticket's Codex conversation (this project's, else the only match) |
| `relay status` | List recent reports and their drafts |
| `relay install-claude` / `relay uninstall-claude` | Add or remove the relay's hooks and commands in Claude |
| `relay install-codex` | Write the relay's part of the reviewer Codex settings |
| `link`, `unlink`, `review-once` | Run by `/relay-link`, `/relay-unlink` and `/relay-review` inside Claude; don't type these yourself |
| `hook-stop`, `hook-session-end`, `mcp` | Run by Claude Code and Codex |
