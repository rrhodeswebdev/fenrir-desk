# web/

The browser UI: Vite + React 19 + TypeScript (strict). Built to `web/dist/`, served by `fenrir serve`.

- Entry points: `src/main.tsx` (boot), `src/App.tsx` (placeholder until the route tree, #37), `src/api.ts` (`apiFetch`, `ApiError`, `wsUrl`: the only code that talks to the server), `src/errorText.ts` (`errorText(e)`: any error as readable text, 401 → "Session expired …").
- Auth: the session is an HttpOnly cookie set by the server's `/launch` link. The page never sees it; same-origin requests carry it. No token in JS, headers or URLs.
- Style: function components + hooks; server state only in TanStack Query (keys `['<area>', ...args]`). Visual rules: root `PRODUCT.md` and `DESIGN.md`. Import from `src` as `@/...`.
- Dev: `uv run fenrir serve --dev` + `npm --prefix web run dev` (port 5173, proxies `/api`, `/ws` and `/launch` to `127.0.0.1:8000`); open the `:5173` launch link the server prints.
- Checks: `npm --prefix web run typecheck`; unit tests `npm --prefix web test -- --run` (Vitest, `src/*.test.ts`); `npm --prefix web run build`.
- TypeScript 7 (native `tsc`). typescript-eslint does not support it: if ESLint is ever added, pin `typescript@~6.0.3`.
- Not here: business logic. Rules, backtests and live trading live in the Python package `src/fenrir/`.
