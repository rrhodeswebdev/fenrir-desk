# web/src/backtests/

The parts of the Backtests page (`../routes/backtests.tsx`) that talk to the backtests API (`docs/contracts.md` "Plan 14 — backtest jobs API").

- Entry points: `RunForm.tsx` (`RunForm`: ticks strategies × accounts × contracts from `GET /api/backtests/options` (query `['backtests', 'options']`) and posts them to `POST /api/backtests/jobs`, then shows the new job id; no contract ticked sends `contracts: null`, every contract of each strategy's market; a 4xx shows via `errorText`; a failed options refetch shows above the form; ticks a refetch removed are dropped by `withinOptions`, never posted). Pure helpers `toggle`, `jobBody`, `withinOptions`, `canRun`, `postJob` and the stateless `RunFormView` are exported for tests.
- Not here: the page shell and route (`../routes/backtests.tsx`), the job list, comparison and report (later tasks), grid expansion and validation (the server).
- Tests: `RunForm.test.tsx` (Vitest, node env, `renderToStaticMarkup`; `fetch` stubbed for the POST body).
