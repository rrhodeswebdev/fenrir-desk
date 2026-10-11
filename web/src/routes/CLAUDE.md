# web/src/routes/

One file per UI page. Each exports `<page>Route` built with `createRoute({ getParentRoute: () => rootRoute, path, component })`, importing `rootRoute` from `./root`.

- Entry points: `root.tsx` (`rootRoute`: the index rail shell, the not-found page, and the "Session expired" line shown while any query's last error is a 401); `../router.tsx` assembles the tree, so add a new route there. A later task replaces a stub's `component` and nothing else in `router.tsx`.
- Pages: `/` workbench, `/rules` (rule files with staleness; `?account=<id>` opens that file in the field-by-field editor beside the list: draft state only through `setIn`, Save → `PUT`, 422 errors under their input by `loc` plus a summary; payout extras and `funded.scaling` round-trip untouched until #54), `/data`, `/backtests`, `/backtests/$key` (`backtest-report.tsx`), `/strategies`, `/live`, `/settings`. Each renders `<Page title strip>` from `@/components/page`.
- Data: `useQuery({ queryKey: ['<area>', ...], queryFn: () => apiFetch(...) })` from `@/api`; mutate with `useMutation` and invalidate the area's keys. No `fetch` outside `../api.ts`. Show errors with `pageError(e)` (null for a 401: the shell's banner reports it).
- Not here: shared components (`../components/`).
- Tests: `root.test.tsx` renders each path with `renderToString` (no DOM); a page with data gets its own `<page>.test.tsx` that seeds the query cache with `setQueryData`; browser flows in `web/e2e/` (#38).
