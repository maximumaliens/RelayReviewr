# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read DESIGN.md

`DESIGN.md` at the repo root holds the domain terms and the decisions (rulings). Read it before working in an area it covers.

Domain terms and decisions are recorded in DESIGN.md. There is no `GLOSSARY.md`, `GLOSSARY-MAP.md` or `docs/adr/`; don't create them. The `/domain-modeling` skill's glossary and ADR output goes into DESIGN.md instead.

## Use DESIGN.md's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as DESIGN.md defines it, not a synonym.

If the concept you need isn't in DESIGN.md yet, either you're inventing language the project doesn't use (reconsider) or there's a real gap (record it in DESIGN.md).

## Flag ruling conflicts

If your output contradicts a ruling in DESIGN.md, surface it explicitly rather than silently overriding:

> _Contradicts the DESIGN.md ruling on upgrading while sessions are held, but worth reopening because…_
