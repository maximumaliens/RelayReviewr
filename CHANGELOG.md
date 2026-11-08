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

### Known limitations
- **Existing conversations keep their old operating instructions (#2, unresolved).** Instructions are given only when a conversation is created, and no tested way updates them. A new ticket label starts a conversation with the current instructions. Reopening the conversation's window or restarting the Codex daemon replaced the tool server in testing. See the README, *After you update the relay*.
