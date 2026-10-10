# design/

Static design prototypes that set the visual system. Nothing here ships or is imported by the app.

- Entry points: `live.html` (Live page + app shell, synthetic data). Open via a local server from the repo root (`python3 -m http.server`), since it loads `../assets/brand/`.
- The system it proves is recorded in root `DESIGN.md` (tokens, components, rules); the web app (`web/`) implements DESIGN.md, not this HTML.
- Direction contract: `.impeccable/surfaces/design-live-html.md`. Product context: root `PRODUCT.md`.
- Not here: production UI code (`web/`), brand source files (`assets/brand/`).
