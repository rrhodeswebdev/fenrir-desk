# docs/

Reference documents. Nothing here runs.

- The v1 source of truth is the root `SPEC.md`, not anything in this directory.
- `contracts.md`: cross-plan contracts (exact names, signatures, file paths, JSON shapes). A contract change lands in the same commit as the code.
- `lucid-mechanics.md`: the Lucid rule semantics `src/fenrir/rules/` assumes, to confirm against Lucid's site.
- `data-gate.md`: LSE data gate findings and the roll-window decision.
- `live-checklist.md`: the manual dry run → sim → live checklist for the live runner (SC4, SC5, gate item 5).
- Work is tracked in GitHub issues; `tasks/plan.md` is the ordered index.
- When code and a spec disagree, the spec is wrong or the code has a bug: ask, don't silently pick one.
