# Changelog

Notable changes to the relay. Releases before this file are described on the GitHub Releases page.

## Unreleased

## v0.2.0 (2026-11-11)

### Added
- **Copied drafts (#2).** `propose_reply` can take `copy` (the whole stored report, or ascending line selections from it) with optional `additions`, instead of free-text `draft`. The relay copies the text itself, so a report is passed on exactly. Invalid combinations are rejected before anything is saved or shown. The menu shows where the copied text came from, and a marked preview for long copied drafts.
- **Isolation guidance (#2).** The reviewer's operating instructions, the `propose_reply` description and each report's header say that sessions are isolated, that a delivered instruction must stand alone, and how to pass on a builder's closeout or a verifier's corrective.
- **No-reply menu (#3).** Codex can call `no_reply_menu` when Claude needs no instruction, so it doesn't draft an acknowledgement that would start another round. The menu offers Discuss, Keep waiting and Finish without sending, recommends Finish, and never sends anything by itself. You get a "review ready — no reply needed" notification, and the no-draft notice doesn't fire for it.
- **Who is on the ticket (#4).** The menu lists every session linked to the ticket in the same project, including one that hasn't reported yet, with the reason it can't be sent to: busy, last turn skipped because background work was running, no turn finished since linking, or released, timed out, finished or failed. Each such session gets Copy for …. Codex gets the same list in each report's header and after each `propose_reply` and `show_menu` result.
- **Copy to clipboard on every menu for a draft (#4),** including after the draft went to another session. That menu still says where the draft went, and copying leaves the delivery record as it is.
- **Copy-only drafts for a finished report (#4).** After Finish, Codex can still prepare a draft for that report with `propose_reply`. Its menu offers Copy and Discuss only; nothing can be sent for it, and the report stays finished.
- **Note to the owner (#4).** `propose_reply` takes an optional `note`. The menu shows it above the draft; it is never sent and is no part of the draft or its file. The reviewer's instructions, the tool description and each report's header say that owner-facing material goes in the note or in chat, never in the draft.

### Changed
- **Test and documentation upkeep (#9); the relay's behaviour is unchanged.** Unit tests no longer launch `powershell.exe` or `clip.exe`, even in processes they start, and Copy tests use an opt-in fake clipboard. Each acceptance run's `summary.md` records the commit and any uncommitted changes. `CLAUDE.md` points to the privacy rules for commits, pull requests and issues, and `docs/development.md` gives the single-file and `-k` test commands.
- **Send needs the draft's source session held (#4, confirming the earlier rule).** When the source has stopped waiting, the menu offers no Send even if another session on the ticket is held; that session is listed with the reason and gets Copy.
- **Discuss says Claude stays held (#3).** The Discuss result, the reviewer instructions, the tool descriptions and the README tell Codex and you to choose Finish first to answer Claude directly.
- Draft files are written as UTF-8 bytes with no newline translation.

### Fixed
- **`/relay-review` sends Claude's last real reply (#5).** It passed on the reply to the relay's own command, such as the "Linked this session…" line after `/relay-link`. It now skips replies to `/relay-link`, `/relay-unlink` and `/relay-review`. If the session has no other reply, or its transcript can't be found, it says so and sends nothing.
- **`/relay-review` no longer unlinks a linked session (#5).** The session keeps its link, label and role, and later replies are reviewed as before. With no label or `--as`, the reply goes to the session's own ticket; if you name a different label or role, only that reply goes there and Claude's reply says the link stays. If the `/relay-review` turn ends while background tasks are running, the one-off is dropped with a "one-off review skipped" notification, and the session's next reply is reviewed through its link as usual; run `/relay-review` again when the tasks finish.

### Known limitations
- **A session continued under a new session ID isn't followed (#4, deferred).** When Claude Code continues a session under a new ID, for example after moving it to the background, run `/relay-link` again in it. The old ID can stay in the menu's list until that session ends or is unlinked.
- **The session list, the note and Copy-only drafts need the tool server replaced in an existing conversation (#4).** Reopen the conversation's window and wait about 15 s, or restart the Codex daemon. The list in each report's header needs nothing.
- **An interrupted `/relay-review` turn (#5).** If you press Esc before the command's turn ends, the reply is sent at the session's next turn end, in place of that turn's own reply.
- **Existing conversations keep their old operating instructions (#2, unresolved).** Instructions are given only when a conversation is created, and no tested way updates them. A new ticket label starts a conversation with the current instructions. Reopening the conversation's window or restarting the Codex daemon replaced the tool server in testing. See the README, *After you update the relay*.
