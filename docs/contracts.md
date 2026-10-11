# Cross-plan contracts

Exact names, signatures, file paths and JSON shapes where modules meet. `SPEC.md` says what and why; this file says exactly how. Implement a contract exactly as written; a change to it lands in the same commit as the code and says so in the commit body. When this file and the spec or code disagree, stop and ask.

"Plan NN" below names the module group that owns a contract. The work itself is tracked as GitHub issues `[NN.x]` (index: `tasks/plan.md`).

## Directory context files

Every directory with its own responsibility gets a `CLAUDE.md` (Claude Code loads it automatically when working there) and a row in the root `CLAUDE.md` → **Directory Map**. The plan that creates a directory adds both, in the same commit as the directory's first code. Covered by the parent's file instead (no file of their own): pure test mirrors (`tests/<pkg>/`), asset-size folders (`png/`), build output.

Template (keep it under ~15 lines):

```markdown
# <path>/

<One sentence: what this directory handles.>

- Entry points: `<file>` (<what it exposes>), ...
- Pure vs shell: <which files are pure core, which do I/O>.
- Not here: <the nearest thing people would wrongly look for here, and where it lives>.
- Tests: `tests/<path>/`.
```


## Contracts

### Plan 01 — `fenrir.core` and settings

```python
# src/fenrir/core/bars.py
@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime        # bar OPEN time, tz-aware UTC; a bar covers [ts, ts+1min)
    open: float
    high: float
    low: float
    close: float
    volume: int
    contract: str       # front-month code the prices came from, e.g. "NQZ26"

# src/fenrir/core/instruments.py
@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str         # "ES" | "MES" | "NQ" | "MNQ"
    series: str         # "ES" | "NQ"  (price series)
    point_value: float  # $ per point: 50, 5, 20, 2
    tick: float         # 0.25
    minis: float        # mini-equivalents per contract: 1.0 for ES/NQ, 0.1 for MES/MNQ

@dataclass(frozen=True, slots=True)
class Roll:
    series: str
    start: date         # first trading day this contract is front month
    contract: str       # "NQZ26"

@dataclass(frozen=True, slots=True)
class Instruments:
    specs: tuple[ContractSpec, ...]
    rolls: tuple[Roll, ...]          # sorted by (series, start)

def load_instruments(path: Path = Path("instruments.yaml")) -> Instruments   # shell
def spec(instruments: Instruments, symbol: str) -> ContractSpec              # KeyError-free: raises ValueError on unknown
def front_month(instruments: Instruments, series: str, day: date) -> str     # ValueError if day is outside the calendar
def parse_instruments(doc: Mapping[str, Any]) -> Instruments                 # pure
def quarterly_rolls(series: str, first_year: int, last_year: int) -> tuple[Roll, ...]  # expiry (3rd Friday) minus 8 days
# Contract codes use a two-digit year (NQZ26) so histories longer than ten years stay unambiguous;
# LSE's own format is mapped only in fenrir.data.lse.lse_symbol. instruments.yaml covers 2008–2030.

# src/fenrir/core/sessions.py   (CME Globex: trading day D runs 18:00 ET on D-1 to 17:00 ET on D)
NY = ZoneInfo("America/New_York")
def trading_day(ts: datetime) -> date
def at_or_after(ts: datetime, hhmm: str, tz: str = "America/New_York") -> bool
    # True iff ts's own trading day has reached hhmm; 18:00–23:59 ET belongs to the next trading day, so it returns False there

# src/fenrir/core/actions.py
Side = Literal["long", "short"]
@dataclass(frozen=True, slots=True)
class Bracket:
    side: Side
    qty: int            # contracts, > 0
    stop: float         # absolute price
    target: float       # absolute price
@dataclass(frozen=True, slots=True)
class Flatten: ...
@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: str
Action = Bracket | Flatten | Cancel

# src/fenrir/settings.py
@dataclass(frozen=True, slots=True)
class Settings:
    lse_api_key: str = ""
    nt_url: str = "http://host.docker.internal:8123"
    nt_token: str = ""
    holdout_months: int = 12
    safety_margin_pct: float = 80.0
    stale_seconds: int = 90
    heartbeat_policy: Literal["keep", "flatten"] = "keep"
    data_dir: Path = Path("data")
    rules_dir: Path = Path("rules")
    strategies_dir: Path = Path("strategies")
def parse_settings(env: Mapping[str, str]) -> Settings      # pure; keys FENRIR_LSE_API_KEY, FENRIR_NT_URL, FENRIR_NT_TOKEN, FENRIR_HOLDOUT_MONTHS, FENRIR_SAFETY_MARGIN_PCT, FENRIR_STALE_SECONDS, FENRIR_HEARTBEAT_POLICY, FENRIR_DATA_DIR, FENRIR_RULES_DIR, FENRIR_STRATEGIES_DIR
def parse_env_text(text: str) -> dict[str, str]             # pure .env parser
def load_settings(env_file: Path = Path(".env")) -> Settings  # shell: .env merged under os.environ
```

CLI: `src/fenrir/cli/main.py` defines `app = typer.Typer(no_args_is_help=True)`. Each later plan adds `src/fenrir/cli/<group>.py` with `app = typer.Typer()` and registers it in `main.py` with one line: `app.add_typer(<group>.app, name="<group>")`. Top-level commands (`backtest`, `evaluate`) register with `app.command("<name>")(<module>.<fn>)` instead. Commands print JSON to stdout when given `--json`.

### Plan 03 — `fenrir.data.store`

```python
def load_bars(data_dir: Path, instruments: Instruments, series: str,
              start: date | None = None, end: date | None = None) -> tuple[Bar, ...]
    # front-month, unadjusted, ascending ts, trading days in [start, end] inclusive
def write_bars(data_dir: Path, series: str, contract: str, bars: Sequence[Bar]) -> None
    # merge by ts (new wins) into data/bars/<series>/1m/<contract>.parquet, refresh manifest.json
def data_version(data_dir: Path) -> str     # sha256 hex of data/bars/manifest.json; changes on every pull
# cost (measured in plan 03's review): a full-series load_bars is ~13 s and ~2.2 GB peak;
# plans 09/10/14 must load each series once per process, keyed by data_version
def coverage(data_dir: Path, series: str) -> Coverage
# roll window (user decision, docs/data-gate.md "Roll mismatch"): load_bars returns no bars and
# coverage counts neither data nor gaps on these days; live trading (plan 21) takes no entries
def fenrir.core.instruments.in_roll_window(day: date) -> bool   # roll Thursday .. Tuesday of expiry week
@dataclass(frozen=True, slots=True)
class Coverage:
    series: str
    first_day: date | None
    last_day: date | None
    days: int
    gaps: tuple[tuple[date, date], ...]   # missing trading-day ranges (weekends/CME holidays excluded)
```

Layout: `data/bars/{ES,NQ}/1m/<contract>.parquet` (columns `ts, open, high, low, close, volume`), `data/bars/manifest.json`. CLI: `fenrir data pull [--series ES --series NQ] [--since YYYY-MM-DD]`, `fenrir data summary [--json]` → `{"data_version": str, "series": [{"series", "first_day", "last_day", "days", "gaps": [[first, last], ...]}]}`. Gap detection uses `pandas-market-calendars` ("CME_Equity").

### Plan 04 — `fenrir.data.live`

```python
@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime; price: float; size: int; contract: str
@dataclass(frozen=True, slots=True)
class BuilderState: ...                     # current partial bar per contract
def empty_builder() -> BuilderState
def feed(state: BuilderState, tick: Tick) -> tuple[BuilderState, tuple[Bar, ...]]   # emits bars closed by this tick
def close_due(state: BuilderState, now: datetime) -> tuple[BuilderState, tuple[Bar, ...]]  # emits bars whose minute has ended
async def stream_bars(settings: Settings, contracts: Sequence[str]) -> AsyncIterator[Bar]   # shell, PROVISIONAL (gate)
```

### Plan 05 — `fenrir.rules.schema` / `loader`

`AccountRules` is a pydantic v2 model, `model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)` (no `"4"` → 4, no inf/nan; ints still fill float fields), mirroring the rules YAML in `SPEC.md` → Rules exactly, plus `max_drawdown.locks_at: float | None` (profit above start at which the trailing floor stops trailing; `null` = never locks). Sub-models: `Drawdown(amount, trailing: Literal["end_of_day","intraday"], locks_at)`, `Consistency(max_day_pct)`, `Payout(max_pct_of_profit, split_pct, min_days, min_profit` (smallest request)`, min_day_profit = 0` (a day counts toward min_days only at or above it)`, max_amount: float|None` (cap per payout)`, max_payouts: int|None)`, `Phase(max_drawdown, daily_loss_limit: float|None, consistency: Consistency|None, min_trading_days: int = 0)` (shared base), `Evaluation(Phase)` adds `profit_target: float`, `Funded(Phase)` adds `payout: Payout` and `scaling: tuple[Tier, ...] | None` (`Tier(from_profit, max_minis)`, ascending from 0; the max size for the current profit, recalculated at session end; `None` = `contracts.max_minis`), `Session(flat_by, tz)`, `Contracts(max_minis, micros_per_mini)`, `Microscalp(max_profit_pct, hold_seconds)`, `Prohibited(microscalp, hft)`, `Commission(ES, MES, NQ, MNQ)`, `Costs(activation_fee, commission_per_side: Commission)` (no `eval_fee`: the price paid varies with coupons, so it is a user input, not a rule) (read a symbol with `getattr(rules.costs.commission_per_side, symbol)`). Top level: `firm, account, verified_on: date | None` (null = not yet verified, shown as stale; ISO strings accepted)`, source_url, automation_allowed, evaluation: Evaluation | None, funded: Funded, session, contracts, prohibited, costs`. Each phase rejects the other's field (`extra="forbid"`). The loader rejects duplicate YAML keys.

```python
def account_id(rules: AccountRules) -> str                 # f"{firm}-{account}", e.g. "lucid-flex-50k"
def load_account(account_id: str, rules_dir: Path) -> AccountRules   # rules/lucid/flex-50k.yaml; ValueError with path + pydantic message
def list_accounts(rules_dir: Path) -> tuple[str, ...]       # sorted ids
def rules_path(account_id: str, rules_dir: Path) -> Path
def dump_account(rules: AccountRules) -> str                # YAML text, stable key order (used by plan 15)
```

### Plan 06 — `fenrir.rules.state` / `account` / `pretrade`

```python
Phase = Literal["evaluation", "funded"]

@dataclass(frozen=True, slots=True)
class AccountState:
    phase: Phase
    day: date                     # current trading day
    balance: float                # realized P&L net of commissions since phase start (starts 0.0)
    open_pnl: float               # unrealized P&L at last mark (close)
    position: int                 # signed net contracts
    peak: float                   # high-water mark the trailing drawdown follows
    floor: float                  # equity (balance + open_pnl) at or below this = breach
    day_start_balance: float
    closed_days: tuple[tuple[date, float], ...]   # (day, net realized P&L) for each completed day with >= 1 fill
    traded_today: bool
    halted_today: bool            # daily loss limit hit this trading day: a day halt, not a breach (Lucid: soft breach)
    possible_microscalp_pnl: float  # profit from trades opened and closed in the same bar

@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime; qty: int        # signed: + buy, - sell
    price: float; commission: float
    realized_pnl: float           # gross $ realized by this fill (0 for opening fills)
@dataclass(frozen=True, slots=True)
class Mark:
    ts: datetime; worst_open_pnl: float; best_open_pnl: float; close_open_pnl: float   # worst/best = at bar's adverse/favorable extreme
@dataclass(frozen=True, slots=True)
class TradeClosed:
    opened: datetime; closed: datetime; pnl: float; same_bar: bool
@dataclass(frozen=True, slots=True)
class DayClosed:
    day: date; ts: datetime
Event = Fill | Mark | TradeClosed | DayClosed

@dataclass(frozen=True, slots=True)
class Breach:
    rule: str        # only "max_drawdown" | "flat_by" (flat_by = an engine failed to flatten by 16:40: an engine
                     # bug, not a Lucid failure; Lucid auto-closes at 16:45). The daily loss limit sets halted_today (no new entries,
                     # engines flatten, cleared at DayClosed); consistency blocks pass/payout,
                     # contract limits are pre-trade rejections, microscalp is a flag.
    reason: str      # human sentence with numbers
    ts: datetime

Rule = Callable[[AccountState, Event], AccountState | Breach]

@dataclass(frozen=True, slots=True)
class AccountView:              # what strategies see in Ctx
    phase: Phase
    drawdown_remaining: float   # equity - floor
    daily_loss_remaining: float | None
    today_pnl: float            # realized today + open
    target_remaining: float | None   # None in funded phase

# Engines (plans 08, 21) emit exactly one DayClosed per trading day, after the flat-by flatten and before
# that day's next event. fenrir.rules.ledger.book() handles accounting: on DayClosed it appends closed_days
# and resets day_start_balance/traded_today/halted_today; Fill/Mark set day = trading_day(ts). Engines also set the new
# day with dataclasses.replace right after DayClosed.

# fenrir.rules.account
def initial_state(rules: AccountRules, phase: Phase, day: date) -> AccountState
def build_rules(rules: AccountRules, phase: Phase) -> tuple[Rule, ...]
def apply(rule_fns: tuple[Rule, ...], state: AccountState, event: Event) -> AccountState | Breach  # first Breach wins
def passed(state: AccountState, rules: AccountRules) -> bool          # evaluation target + consistency + min days
def payout(state: AccountState, rules: AccountRules) -> float | None  # trader's first payout $ after split, or None if not yet eligible
    # funded, flat, balance > 0; closed days >= min_day_profit count toward min_days; consistency on closed days;
    # request = balance x max_pct_of_profit%, capped at max_amount, None if < min_profit; returns request x split_pct%
def view(state: AccountState, rules: AccountRules) -> AccountView
def with_dll(rules: AccountRules, dll_on: bool) -> AccountRules
    # effective rules: dll_on False → every phase's daily_loss_limit = None; True with a null amount in a present
    # phase → ValueError ("DLL is on but the rules file has no daily_loss_limit amount for <phase>").
    # Engines apply it once after load_account; everything else takes the result as-is.

# Calling order (engines, plans 08/21): per bar, apply the Fill events, then the bar's Mark, and only then call
# view/check, so open_pnl is fresh after a partial close (book keeps open_pnl until the next Mark). Call payout
# after DayClosed (its request uses balance, its eligibility closed days). Drawdown and the daily loss limit are
# checked on Mark/DayClosed, not on Fill, so a breach is stamped with that bar's time.

# fenrir.rules.pretrade
def check(state: AccountState, rules: AccountRules, bracket: Bracket, spec: ContractSpec,
          entry_price: float, held_minis: float) -> str | None
    # rejection reason if halted_today, if stop-out (incl. 1 tick slippage + round-trip commission) would breach
    # drawdown or daily loss limit, or held_minis + qty*spec.minis > max_minis(rules, state); None = accept

# fenrir.rules.contracts
def max_minis(rules: AccountRules, state: AccountState) -> int  # funded.scaling tier for max(0, day_start_balance), else contracts.max_minis
def contract_limit(limit: int, held_minis: float, add_minis: float) -> str | None
```

### Plan 07 — `fenrir.strategy`

```python
@dataclass(frozen=True, slots=True)
class Position:
    qty: int                     # signed contracts; 0 = flat
    entry: float | None
    stop: float | None
    target: float | None
@dataclass(frozen=True, slots=True)
class Ctx:
    ts: datetime                 # current bar's open time
    bars: tuple[Bar, ...]        # last <= 500 bars, ending with the current bar
    position: Position
    account: AccountView
    spec: ContractSpec           # the contract being traded (tick, point_value)

@dataclass(frozen=True, slots=True)
class Strategy:
    name: str                    # file stem
    path: Path
    hash: str                    # sha256 hex of file bytes
    market: str                  # "ES" | "NQ"
    params: Mapping[str, Any]
    init: Callable[[Mapping[str, Any]], Any]
    step: Callable[[Any, Bar, Ctx], tuple[Any, list[Action]]]
# Position, Ctx in fenrir.strategy.types; Strategy, load_strategy, file_hash in fenrir.strategy.loader
def load_strategy(path: Path) -> Strategy          # ValueError if MARKET/PARAMS/init/step missing, MARKET invalid, or I/O imports
def file_hash(path: Path) -> str
```

### Plans 09/10 — `fenrir.backtest`

```python
# fenrir.backtest.run
def run_backtest(strategy_path: Path, account_id: str, contract: str, settings: Settings, *,
                 include_holdout: bool, on_progress: Callable[[int, int], None] | None = None) -> Report
    # reads the account's dll_on from data/accounts.yaml and runs with_dll(load_account(...), dll_on)
class AccountChoice(BaseModel):   # frozen, extra="forbid", strict; the user's own choices, not firm rules
    eval_price: float = 0.0       # >= 0, finite: what the user paid for the evaluation (varies with coupons)
    dll_on: bool = False          # Lucid's daily loss limit turned on for this account
def load_account_choices(data_dir: Path) -> dict[str, AccountChoice]
    # shell: <data_dir>/accounts.yaml, account id → {eval_price, dll_on}; {} when absent/empty; missing account or
    # key = defaults; ValueError naming the file on bad YAML, non-string keys, unknown keys or bad values
def net_per_attempt(period: PeriodResult, account_id: str, choices: Mapping[str, AccountChoice]) -> float
    # pure: expected_gross_per_attempt − eval_price. Every net figure shown, ranked or compared uses this,
    # with choices loaded once per command/request. Never stored in a report.
# fenrir.backtest.report
def report_key(strategy_hash, rules_hash, data_version, contract, holdout_months, include_holdout, dll_on) -> str
    # cache_key = report_key over the file sha256s and the account's dll_on; eval_price is never in it
# fenrir.backtest.grid
@dataclass(frozen=True, slots=True)
class GridItem:
    strategy_path: Path; account_id: str; contract: str
def expand_grid(strategy_globs: Sequence[str], account_globs: Sequence[str],
                contracts: Sequence[str] | None, settings: Settings) -> tuple[GridItem, ...]
    # contracts None = both symbols of the strategy's MARKET (NQ → NQ, MNQ)
# fenrir.backtest.cache
def cache_key(item: GridItem, settings: Settings, include_holdout: bool) -> str   # sha256(strategy bytes, rules bytes, data_version, contract, holdout_months, include_holdout, dll_on from data/accounts.yaml); not eval_price
def cached_run(item: GridItem, settings: Settings, *, include_holdout: bool,
               on_progress: Callable[[int, int], None] | None = None) -> Report   # reads/writes data/results/<key>.json
def list_reports(settings: Settings) -> tuple[Report, ...]                        # newest first
# fenrir.backtest.compare (pure; ranks by tuning results only, hold-out never changes the order)
View = Literal["account", "strategy"]
def summarize(report: Report) -> dict             # the report without sample_equity in any period
def with_net(report: Report, choices: Mapping[str, AccountChoice]) -> dict
    # every non-null period also carries expected_net_per_attempt (net_per_attempt)
def latest_only(reports: Sequence[Report], choices: Mapping[str, AccountChoice]) -> tuple[Report, ...]
    # input newest first; drops reports whose dll_on differs from the account's current choice, then keeps
    # the newest per (strategy, account, contract), in input order; a newest report without a hold-out gives
    # way to an older one with a hold-out and the same strategy_hash, rules_hash, data_version, holdout_months
def compare(reports: Sequence[Report], view: View, choices: Mapping[str, AccountChoice]) -> list[dict]
    # [{"group": account or strategy name, "rows": [with_net(summarize(r)), ...]}]; groups sorted by name;
    # rows by tuning p_pass_and_payout desc, then tuning net per attempt desc, then strategy, account, contract
```

`Report` is a `TypedDict` in `fenrir.backtest.report`, serialized as JSON:

```json
{
  "key": "<cache key>",
  "created_at": "2026-10-10T14:00:00Z",
  "strategy": "orb", "strategy_hash": "<sha256>", "params": {},
  "account": "lucid-flex-50k", "rules_hash": "<sha256>",
  "contract": "MNQ", "data_version": "<sha256>",
  "holdout_months": 12,
  "dll_on": false,
  "periods": {
    "tuning":  { "...PeriodResult" },
    "holdout": { "...PeriodResult" }
  },
  "holdout_gap": false,
  "note": "Attempts start on every trading day and overlap, so these rates are estimates, not exact probabilities."
}
```

`periods.holdout` is `null` when `include_holdout` is false. `holdout_gap` is true when held-out `p_pass_and_payout` < 50% of tuning's. `PeriodResult`:

```json
{
  "start": "2019-01-02", "end": "2025-10-09", "attempts": 1700,
  "pass_rate": 0.31,
  "days_to_pass": { "median": 9, "histogram": [[1, 4], [2, 11]] },
  "failure_reasons": { "max_drawdown": 900, "flat_by": 3, "no_result": 20 },
  "payout_rate": 0.62,
  "median_days_to_payout": 14, "median_first_payout": 1250.0,
  "p_pass_and_payout": 0.19,
  "expected_gross_per_attempt": 87.5,
  "microscalp_flags": 4,
  "sample_equity": [ { "start": "2023-05-01", "outcome": "payout", "points": [["2023-05-01T13:31:00Z", 0.0]] } ]
}
```

`failure_reasons` keys are `"max_drawdown"`, `"flat_by"` and `"no_result"` (data ran out first); it counts every attempt without a payout. Hitting the daily loss limit is not a failure: that day halts and the attempt continues. `median_days_to_payout` counts from the attempt's start. `expected_gross_per_attempt` = mean over attempts of (payout or 0) − (activation_fee if passed); fee-free, because the evaluation price varies with coupons and is user data (`AccountChoice.eval_price`). Net per attempt = gross − that price, computed on read (`net_per_attempt`) and added by the API as `expected_net_per_attempt`; it is never in a cached report. `dll_on` records whether the daily loss limit was simulated (the user's choice, part of the key). One live/backtest rule: a new `Bracket` is rejected while a position is open. Medians are `null` when there is nothing to take a median of. `sample_equity` holds at most 6 attempts (2 passed+paid, 2 passed only, 2 breached; first by start date).

CLI: `fenrir backtest <strategy globs...> --account <glob> [--account ...] [--contract MNQ ...] [--json]` (tuning only); `fenrir evaluate <same>` (tuning + hold-out); `fenrir compare --view account|strategy [--json]` prints `compare(latest_only(list_reports(settings), choices), view, choices)` (`--json`: `[{group, rows}]`).

### Plan 11 — server

- `fenrir.server.app.create_app(settings: Settings, token: str, static_dir: Path | None, *, port: int = 8000, dev: bool = False) -> FastAPI`. Stores `app.state.settings`; handlers read `request.app.state.settings`. `port` sets allowed WebSocket Origins; `dev` adds Vite origins (`:5173`) and `/api/dev-token`. Requests whose Host is not `127.0.0.1`/`localhost` get 400 (DNS-rebinding guard).
- `fenrir serve [--port 8000] [--dev] [--static-dir web/dist]` binds `127.0.0.1`, or `0.0.0.0` only when `FENRIR_BIND_ALL=1` (set by the Docker image alone); generates `token = secrets.token_urlsafe(32)` per start.
- Token reaches the UI as `<meta name="fenrir-token" content="...">` injected into the served `index.html`. REST: header `X-Fenrir-Token`; missing or bad → 401. WebSocket: `?token=` query param; the upgrade is rejected (close code 4401) on a bad token or an `Origin` that is not `http://127.0.0.1:<port>` or `http://localhost:<port>`.
- `fenrir.server.auth.require_token` is a FastAPI dependency applied to every `/api` router; `fenrir.server.auth.check_ws(ws) -> bool` for WebSocket routes.
- Routes: one module per area in `src/fenrir/server/routes/<area>.py` exposing `router = APIRouter(prefix="/api/<area>")`; WebSockets at `/ws/<name>`. Each plan registers its router in `create_app` with one line.
- `GET /api/health` → `{"ok": true}`; the only unauthenticated route.
- WebSocket routers (`/ws/*`) are registered without `require_token`; each handler calls `check_ws` before `accept()`.
- The PTY child environment is built by a pure function that drops every `FENRIR_*` var and the token (plan 13).

### Plan 12 — web

- `web/src/api.ts`: `apiFetch<T>(path: string, init?: RequestInit): Promise<T>` (adds `X-Fenrir-Token`, throws `ApiError` with status + body text on non-2xx), `wsUrl(path: string): string` (adds `?token=`), `token(): string`. `ApiError` has `status: number` and `body: string`. `web/src/errorText.ts` exports `errorText(e: unknown): string` (plans 14–16).
- Routes: `web/src/routes/<page>.tsx`, each exports `<page>Route` created with `createRoute` and imports `import { rootRoute } from './root'`; `web/src/router.tsx` assembles the tree. Pages: `/` Workbench, `/rules`, `/data`, `/backtests`, `/backtests/$key`, `/strategies`, `/live`, `/settings`.
- Query keys: `['<area>', ...args]`, e.g. `['backtests', 'results']`.
- Styling: implements root `DESIGN.md` exactly. Dark only: DESIGN.md's token block sits in `:root` of `web/src/index.css` (no `.dark` class, no light palette, `@custom-variant dark` always matches); radius 0; no shadows; colour only for abnormal or selected state (Steel normal, Ember caution/selected, Red halt/breach/destructive; never green). Tailwind colours: `ground`, `ground-deep`, `rule`, `rule-strong`, `idle`, `ink`, `ink-2`, `ink-3`, `ember`, `red` plus shadcn's variables. Utilities in `index.css`: `label` (11px tracked mono caps, Slate), `value` (13px mono figure), `step` (90ms `steps(2)` transition). Base `h1` = page title, `h2`/`h3` = label. Fonts: `font-sans`/`font-mono` = IBM Plex Mono, `font-display` = Chakra Petch, imported from `@fontsource` in `main.tsx`. Canvas widgets (xterm, lightweight-charts) read colours from the CSS variables via `getComputedStyle`. No hand-written CSS classes outside `index.css`; no inline styles except runtime-computed values.
- Shell: `rootRoute` renders the index rail (plain `<nav>`, not shadcn Sidebar; active page = boxed cell with an Ember block) and `<main>`. Every page renders `<Page title="…" strip={…}>` from `web/src/components/page.tsx` (top strip with the `h1`, optional strip content such as the kill switch, then the body; `className` overrides body padding). `web/src/components/state-text.tsx` exports `StateText({ tone: 'normal' | 'idle' | 'caution' | 'alarm', children })` for one-word statuses (replaces Badge).
- Components: import as `@/components/ui/<name>`, `cn()` from `@/lib/utils`. Each plan adds the components it first needs with `cd web && npx shadcn@latest add <names>` (answer No if one already exists). `Button` variants are replaced (plan 12) by DESIGN.md's control: `outline` (default, plain), `destructive` (red edge block: kill, resolve, destructive confirms), `caution` (ember edge block: arm live); sizes `default`, `sm`, `icon`; no filled variant. Confirmations (arming live, disarm, resolve, kill switch) use `AlertDialog` (destructive confirm: `className={buttonVariants({ variant: 'destructive' })}`); tables `Table`; view switches `Tabs`; forms `Input`/`Label`/`Checkbox`/`Select`/`Switch`. No `Badge`, `Card` or `Progress`: statuses are `StateText` or a code strip, panels are hairline-divided sections or sleeves, meters are segmented. The rules form does not use shadcn's `Form` (react-hook-form/zod): it keeps the pure `setIn` state and server-side validation.
- Dev: `npm --prefix web run dev` proxies `/api` and `/ws` to `127.0.0.1:8000`; in dev the token comes from `GET /api/dev-token`, only registered when `fenrir serve --dev`.
- Playwright tests in `web/e2e/`, run against `fenrir serve` with fixture data dir `tests/fixtures/e2e/`.

### Plan 14 — backtest jobs API

- `GET /api/backtests/options` → `{strategies: string[], accounts: string[], contracts: string[]}`.
- `POST /api/backtests/jobs` `{strategies: string[], accounts: string[], contracts: string[] | null, include_holdout?: boolean (default true)}` → `{job_id}`; 422 for a strategy outside `strategies/`.
- `GET /api/backtests/jobs` → `Job[]`; `GET /api/backtests/jobs/{id}` → `Job` where `Job = {id, created_at, include_holdout, items: {strategy, account, contract, status: "pending"|"running"|"done"|"error", key: string|null, error: string|null, progress: [done, total]}[]}`. Jobs persist in `data/jobs/<id>.json`; on server start, unfinished items re-run. `app.state.jobs` is a `JobRunner`; `fenrir serve` calls `app.state.jobs.start()`.
- `WS /ws/jobs` pushes `Job` on every change.
- `GET /api/backtests/results` → `Report[]` (without `sample_equity`); `GET /api/backtests/results/{key}` → full `Report`. In these and `/compare`, every period also carries `expected_net_per_attempt` (`fenrir.backtest.compare.with_net`, from `data/accounts.yaml` read per request).
- `GET /api/backtests/compare?view=account|strategy` → `[{group, rows: Report[]}]` = `compare(latest_only(list_reports(settings), choices), view, choices)` (`fenrir.backtest.compare`, plan 10), ranked by tuning results only: `p_pass_and_payout`, then net per attempt.

### Plan 15 — rules API

- `GET /api/rules` → `[{id, verified_on, age_days, stale, error}]` (stale = null or > 30 days); `GET /api/rules/{id}` → that row plus `rules: object | null` (`AccountRules.model_dump(mode="json")`, null when the file does not validate); `PUT /api/rules/{id}` body = the `rules` object → 200 row | 404 | 422 `{detail, errors: [{loc: (string|number)[], msg}]}` (pydantic `loc`; a rename is an error at `["account"]`; atomic write via `dump_account`). The UI is a field-by-field form (`web/src/rulesForm.ts`, pure `setIn`). e2e runs serve `FENRIR_RULES_DIR=tests/fixtures/e2e/rules`, a fresh gitignored copy of `rules/`.

### Plan 16 — data API

- `GET /api/data` → `{series: Coverage[], rolls: Roll[], front: {ES, NQ}, pull}`; `POST /api/data/pull` → 202, or 409 while a pull runs (one `fenrir data pull` child process at a time).

### Plan 19/20 — add-on HTTP API (base URL `Settings.nt_url`, header `X-Fenrir-Token: Settings.nt_token`)

| Method | Path | Body → Response |
|---|---|---|
| GET | `/accounts` | → `[{id, name, is_sim, balance, realized_pnl_today, unrealized_pnl}]` |
| GET | `/positions?account=ID` | → `[{account, instrument, qty, avg_price}]` (qty signed) |
| GET | `/orders?account=ID` | → `[{account, order_id, client_id, instrument, role: "entry"\|"stop"\|"target", action: "buy"\|"sell", qty, price, state}]` |
| POST | `/bracket` | `{client_id, account, instrument, side, qty, stop, target}` → `{accepted: bool, duplicate: bool, error: string\|null}` |
| POST | `/flatten` | `{client_id, account}` → `{accepted, duplicate, error}` |
| POST | `/cancel` | `{client_id, account, order_id}` → `{accepted, duplicate, error}` |
| POST | `/flatten-all` | `{client_id}` → `{accepted, duplicate, error}` |
| WS | `/stream?token=` | server → `{type: "heartbeat", ts}` every 2 s; `{type: "account", ...account}`; `{type: "position", ...position}`; `{type: "order", ...order}`; `{type: "fill", account, client_id, order_id, instrument, qty, price, commission, ts}` |

Instrument names are NinjaTrader's (`"NQ 12-26"`). Wrong token → HTTP 401 / WS close. Entry is a market order; stop and target are OCO. `/orders` returns working orders only; `role` adds `"other"` for orders not placed by Fenrir. Fill `qty` is signed (+ buy, − sell). `ts` is UTC ISO 8601 with milliseconds and `Z`. `/flatten-all` flattens and cancels on every account.

```python
# fenrir.bridge.names
def nt_instrument(symbol: str, front_code: str) -> str   # ("MNQ", "NQZ26") -> "MNQ 12-26"
# fenrir.bridge.client  (shell; async httpx + websockets)
class AddonClient: accounts(), positions(account), orders(account), bracket(...), flatten(...), cancel(...), flatten_all(...), stream() -> AsyncIterator[dict]
    # accounts() returns list[dict] in /accounts shape; raises httpx.HTTPError when NinjaTrader is unreachable.
    # stream() never ends: yields {"type":"connected"} after each (re)connect and {"type":"disconnected","error":str}
    # on a drop, bad token or missed heartbeats (default 6 s). Plan 22 sets app.state.addon: AddonClient.
# fenrir.bridge.reconcile
def reconcile(expected: ExpectedBook, positions: Sequence[NtPosition], orders: Sequence[NtOrder]) -> tuple[Mismatch, ...]
```

### Plans 21–24 — live API and storage

SQLite at `data/fenrir.db`, owned by `fenrir.live.journal` (plan 21): tables `journal(id, ts, account, kind, payload_json)` with `kind ∈ {"bar","action","rejection","fill","alert","halt"}`, and `armed(account PRIMARY KEY, strategy_path, strategy_hash, rules_account, contract, mode, phase, armed_at)` with `phase ∈ {"evaluation","funded"}`. `fenrir.live.journal.open_db(path: Path) -> sqlite3.Connection` creates both tables if missing. `src/fenrir/live/arming.py` is created by plan 21 (`Armed`, `list_armed`, `insert_armed`, `delete_armed`); plan 23 adds `ArmRequest`, `Refusal`, `decide` to that file. The runner re-reads `armed` on every closed bar and halts an account whose strategy file hash ≠ `armed.strategy_hash`.

- `GET /api/live/status` → `{lse: {connected, last_bar_ts}, nt: {connected, last_heartbeat}, drift: {ok: boolean, ticks: number|null}, accounts: AccountStatus[]}`, `AccountStatus = {account, mode: "dry"|"sim"|"live", phase: "evaluation"|"funded", strategy, rules_account, contract, halted: boolean, halt_kind: "safety"|"reconcile"|"stale"|"drift"|"breach"|null, halt_reason: string|null, balance, today_pnl, limits: {drawdown_remaining, drawdown_max, daily_loss_remaining: number|null, daily_loss_max: number|null, max_minis, held_minis}, position: {qty, entry, stop, target}, session: {start: number (equity at session start, relative to phase start), floor: number, target: number|null, safety: number|null (equity level of the safety margin for today, null when no daily loss limit), equity: [ts, number][] (one point per closed bar this trading day, close mark), fills: string[] (ISO ts of today's fills)}}`.
  - `halted` is true for any halt and for a stale or drift pause (the brief `unreconciled` pause while connecting to NinjaTrader appears only in `halt_reason`); `halt_kind` names it (`safety` margin or daily loss limit day halt, `reconcile` mismatch, `stale` feed pause, `drift` LSE-vs-NinjaTrader pause, `breach`), and is null for other halts (strategy error, strategy file changed, mode mismatch, cannot start). Plan 21 sets `safety`, `stale`, `breach`; plan 22 sets `reconcile`, `drift` and fills `drift` (`ticks` = largest last entry-fill distance from the LSE close; plan 21 reports `{ok: true, ticks: null}`).
  - An account that cannot start has `balance`, `today_pnl`, `limits`, `position` and `session` = null; `halt_reason` says why.
- Code strips (plan 24, DESIGN.md): the **system strip** is Feed (alarm when `lse.connected` is false, caution when any account has `halt_kind == "stale"`), Broker (alarm when `nt.connected` is false — the add-on stream reports a missed heartbeat as disconnected; idle while every account is a dry run), Drift (caution when `drift.ok` is false). **Each account strip** is Risk (caution when `halt_kind == "safety"` or equity `session.start + today_pnl` is at or below `session.safety`; alarm when `halt_kind == "breach"`) and Book (alarm when `halt_kind == "reconcile"`). Stale and drift pauses light the system cell, not the account. Beside the system strip, the page names the most severe account needing action as `<cell> · <account>`, e.g. "Book · LFE 0911" (other halts use "Halt").
- `GET /api/live/journal?account=ID&day=YYYY-MM-DD` → `JournalRow[]` (`{id, ts, account, kind, payload}`).
- `POST /api/live/kill` → `{ok, error}`: flatten all, cancel all, disarm all (plan 22).
- `POST /api/live/resolve` `{account}` → `{ok}` | 404 when the account is not running: clears a reconciliation halt after the user fixed it (plan 22).
- `GET /api/strategies` → `[{name, path, hash, market, params, error, reports: Report[] (without sample_equity, matching current hash, periods with `expected_net_per_attempt`)}]` (plan 23); a file that fails to load has `error` set, `market: null`, `params: {}`, `reports: []`.
- `GET /api/arming` → `Armed[]`; `GET /api/arming/accounts` → NinjaTrader accounts `[{id, name, is_sim, ...}]` | `502 {error: "nt_unavailable"}`; `POST /api/arming/arm` `{account, strategy_path, rules_account, contract, mode, phase, confirm: boolean}` → `200 Armed` | `409 {error: "no_backtest"|"hash_changed"|"needs_confirmation"|"already_armed"|"unknown_account"|"mode_mismatch"|"no_evaluation_phase"|"dll_mismatch", summary?: PeriodResult & {expected_net_per_attempt}}` | `400` (strategy path outside `strategies/` or unknown `rules_account`) | `502 {error: "nt_unavailable"}`. A matching backtest has the same `strategy_hash`, `account` (= `rules_account`) and `contract`, and its `dll_on` equals the account's current `dll_on` in `<data_dir>/accounts.yaml` (else `dll_mismatch`: re-run the backtest); `POST /api/arming/disarm` `{account}` (plan 23). No CLI command arms or disarms.
- `GET /api/settings/accounts` → `{[account_id]: {eval_price, dll_on}}` for every account in `rules/` (defaults filled in); `PUT /api/settings/accounts` partial map → merged into `<data_dir>/accounts.yaml` (user data, not `.env`, not `rules/`); 422 on a bad value, unknown key or unknown account (plan 24). Prices apply at once; `dll_on` applies to new backtests and to accounts started after saving.
- `GET /api/settings` → settings with secrets masked (`"••••1234"`); `PUT /api/settings` partial update writes `.env` in place (comments and unknown lines preserved); changes apply on restart; `GET` reads `.env` fresh (plan 24). Docker passes `.env` only via bind mount, never `env_file`, so edits are not shadowed by os.environ.
