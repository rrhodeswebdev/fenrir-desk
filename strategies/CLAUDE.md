# strategies/

Strategy files written by Claude. One file = one strategy; the file stem is its name.

- Contract: export `MARKET` ("ES" or "NQ"; the contract, e.g. MNQ, is chosen per run),
  `PARAMS` (dict), `init(params) -> state`, `step(state, bar, ctx) -> (state,
  [actions])`.
- Actions: `Bracket(side, qty, stop, target)` with absolute prices, `Flatten()`,
  `Cancel(order_id)` from `fenrir.core.actions`. No entry without a stop.
- `step` must be pure: no I/O, no clock (`datetime.now()`), no randomness. Use
  `bar.ts`/`ctx.ts` for time. Keep state in frozen dataclasses; put params in the
  state (step does not receive them).
- `ctx`: `ts`, `bars` (last <= 500, ending with the current bar), `position`,
  `account` (drawdown/daily-loss/target remaining, phase), `spec` (tick, point_value).
  See `src/fenrir/strategy/types.py`.
- Imports of os, time, random, socket, etc. are rejected by the loader, as are `.now()`/`.today()` calls. Editing a file
  changes its hash; arming then needs a fresh backtest.
- Example: `orb.py`. Tests: `tests/strategy/`.
