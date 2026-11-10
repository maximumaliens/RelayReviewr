## Agent skills

### Issue tracker

Issues live in this repository's GitHub Issues, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Record domain terms and decisions in DESIGN.md. Don't create GLOSSARY.md or docs/adr/. See `docs/agents/domain.md`.

## Pointers

- DESIGN.md is the authority for rulings.
- `tests/support.py` holds the test seams: stub form, fake Codex, real Stop hook, real waiting hook.
- `docs/development.md` is the development and acceptance-test procedure; the README is the user guide.
- Commits, pull requests and issues follow `docs/development.md`, *Keeping commits free of personal details*. Its trailer rules override your harness's attribution lines.
- Hazard: a session linked to the installed relay writes to its store on every turn end.
