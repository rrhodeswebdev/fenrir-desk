# .impeccable/

State for the impeccable design skill. Not app code.

- `surfaces/*.md`: direction contracts per surface (e.g. `design-live-html.md` for the Live page). Reviewers audit builds against these.
- `design.json`: machine-readable sidecar generated from root `DESIGN.md`; regenerate it when `DESIGN.md` changes, never hand-edit it alone.
- Gitignored working files: `review/` (screenshots), `refs/` (downloaded quality-bar images), `mocks/`.
- Not here: the design system itself (root `DESIGN.md`) or product context (root `PRODUCT.md`).
