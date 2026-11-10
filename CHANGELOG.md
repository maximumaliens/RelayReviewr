# Changelog

Notable changes to the relay. Releases before this file are described on the GitHub Releases page.

## Unreleased

### Added
- **Copied drafts (#2).** `propose_reply` can take `copy` (the whole stored report, or ascending line selections from it) with optional `additions`, instead of free-text `draft`. The relay copies the text itself, so a report is passed on exactly. Invalid combinations are rejected before anything is saved or shown. The menu shows where the copied text came from, and a marked preview for long copied drafts.
- **Isolation guidance (#2).** The reviewer's operating instructions, the `propose_reply` description and each report's header say that sessions are isolated, that a delivered instruction must stand alone, and how to pass on a builder's closeout or a verifier's corrective.
- **No-reply menu (#3).** Codex can call `no_reply_menu` when Claude needs no instruction, so it doesn't draft an acknowledgement that would start another round. The menu offers Discuss, Keep waiting and Finish without sending, recommends Finish, and never sends anything by itself. The no-draft notice doesn't fire for it.

### Changed
- **Discuss says Claude stays held (#3).** The Discuss result, the reviewer instructions, the tool descriptions and the README tell Codex and you to choose Finish first to answer Claude directly.
- Draft files are written as UTF-8 bytes with no newline translation.

### Fixed
- **`/relay-review` sends Claude's last real reply (#5).** It passed on the reply to the relay's own command, such as the "Linked this session…" line after `/relay-link`. It now skips replies to `/relay-link`, `/relay-unlink` and `/relay-review`. If the session has no other reply, or its transcript can't be found, it says so and sends nothing.
- **`/relay-review` no longer unlinks a linked session (#5).** The session keeps its link, label and role, and later replies are reviewed as before. With no label or `--as`, the reply goes to the session's own ticket; if you name a different label or role, only that reply goes there and Claude's reply says the link stays.

### Known limitations
- **A `/relay-review` turn that ends with background tasks running, or is interrupted (#5).** The reply is sent at the session's next turn end without them. In a linked session it's sent in place of that turn's own reply, which isn't reported.
- **Existing conversations keep their old operating instructions (#2, unresolved).** Instructions are given only when a conversation is created, and no tested way updates them. A new ticket label starts a conversation with the current instructions. Reopening the conversation's window or restarting the Codex daemon replaced the tool server in testing. See the README, *After you update the relay*.
