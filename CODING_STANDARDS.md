# Coding standards

The reviewer checks every change against these rules.

- **Upgrade-safe.** Hooks run for up to 4 h and the menu server keeps running, both on the code they started with. A change to what they poll or write keeps old processes correct. DESIGN.md, "Upgrading while sessions are held", is the pattern.
- **Conditional status changes.** Every hand-off or draft status change passes `only_from`. An unconditional overwrite carries a comment saying why it is intended.
- **Notices stay outside state changes.** A notification or toast never sits inside a block whose failure changes a hand-off's state.
- **One naming path.** Everything shown to the user or Codex that names a session goes through `store.session_name` / `store.display_name`.
