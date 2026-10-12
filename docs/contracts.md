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
    data_dir: Path = Path("data")
    rules_dir: Path = Path("rules")
    strategies_dir: Path = Path("strategies")
    private_dir: Path = Path("data/private")
def parse_settings(env: Mapping[str, str]) -> Settings      # pure; keys FENRIR_LSE_API_KEY, FENRIR_NT_URL, FENRIR_NT_TOKEN, FENRIR_HOLDOUT_MONTHS, FENRIR_SAFETY_MARGIN_PCT, FENRIR_STALE_SECONDS, FENRIR_DATA_DIR, FENRIR_RULES_DIR, FENRIR_STRATEGIES_DIR, FENRIR_PRIVATE_DIR (see Plan 11)
def parse_env_text(text: str) -> dict[str, str]             # pure .env parser
def env_file_path(environ: Mapping[str, str]) -> Path | None  # pure; $FENRIR_ENV_FILE, else .env, else None when FENRIR_BIND_ALL=1
def load_settings() -> Settings                             # shell: env_file_path(os.environ) merged under os.environ
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
    # or when it is a symlink, not a regular file or over 1 MiB (read via fenrir.files.read_regular, DEC-1);
    # errors give line/field only, never the file's text
def net_per_attempt(period: PeriodResult, account_id: str, choices: Mapping[str, AccountChoice]) -> float
    # pure: expected_gross_per_attempt − eval_price. Every net figure shown, ranked or compared uses this,
    # with choices loaded once per command/request. Never stored in a report.
def release() -> None
    # shell: shut the shared spawn pool down and clear the memoized inputs. run_backtest keeps both
    # between runs (one at a time per process, under a lock); a long-lived process (#43) calls this
    # when its queue is idle so idle workers stop holding a series
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

- `fenrir.server.app.create_app(settings: Settings, session_key: str, launch_code: str, static_dir: Path | None, *, port: int = 8000, dev: bool = False) -> FastAPI`. Stores `app.state.settings`; handlers read `request.app.state.settings`. `port` sets allowed WebSocket Origins; `dev` adds Vite origins (`:5173`). Requests whose Host is not `127.0.0.1`/`localhost` get 400 (DNS-rebinding guard).
- `fenrir serve [--port 8000] [--dev] [--static-dir web/dist]` binds the loopback addresses `127.0.0.1` and `::1` (`localhost` resolves to `::1` first on macOS, so leaving it free would let any local process catch the launch link), or `0.0.0.0` only when `FENRIR_BIND_ALL=1` exactly (set by the Docker image alone, whose compose file publishes the port on host loopback; plan 18.1, pure `bind_hosts(environ) -> tuple[str, ...]`). Bound to `0.0.0.0` the Host guard still refuses any Host but `localhost`/`127.0.0.1`, and the link still prints `localhost`. Both sockets are bound before the launch link prints; if either fails it exits 1, except that a host with no IPv6 loopback (`EADDRNOTAVAIL`/`EAFNOSUPPORT`) gets `127.0.0.1` alone and a warning. A `[::1]` Host still gets 400. An existing `session.key` must be a regular file, not a symlink, owned by the server's euid, with no group or other permission bits and at least 32 characters, else it exits 1. It refuses (exit 1) a `--static-dir` that equals or contains `private_dir` or holds a `.env*` file, since static files are served without the cookie; without `<static-dir>/index.html` it prints "UI not built" and serves the API only. It loads `<private_dir>/session.key`, creating it (`secrets.token_urlsafe(32)`, mode 0600) when missing, so sessions survive restarts; makes a fresh `launch_code = secrets.token_urlsafe(16)` per start and prints `Open http://localhost:<port>/launch?code=<code>` (with `--dev`, the `:5173` URL). Natively it also prints that the Workbench terminal runs as the current user and is not isolated (DEC-1).
- Session (DEC-1): `GET /launch?code=` (unauthenticated) with the unused launch code sets cookie `fenrir_session=<session_key>` (HttpOnly, SameSite=Strict, Path=/, Max-Age 400 days) and answers 303 to `/`; the code is then spent. Any other code → 403. Nothing else ever returns the session key: no meta tag, header or query parameter. Requests to Host `127.0.0.1` are redirected (307) to the same path on `localhost`, so the cookie has one host. REST: every `/api` route except `/api/health` needs the cookie; missing or bad → 401. Every non-GET `/api` request whose `Origin` is missing or not in the same allowed set the WebSocket check uses (`http://localhost:<port>`, `http://127.0.0.1:<port>`, plus the `:5173` origins in dev), or whose `Sec-Fetch-Site` is present and not `same-origin`, → 403. WebSocket (DEC-2): a missing or bad cookie, or an `Origin` that is not `http://127.0.0.1:<port>` or `http://localhost:<port>`, is refused before `accept()`, which uvicorn answers with HTTP 403.
- `fenrir.server.auth.require_session` is a FastAPI dependency applied to every `/api` router; `fenrir.server.auth.check_ws(ws) -> bool` for WebSocket routes. Key and code comparisons use `secrets.compare_digest`.
- Routes: one module per area in `src/fenrir/server/routes/<area>.py` exposing `router = APIRouter(prefix="/api/<area>")`; WebSockets at `/ws/<name>`. Each plan registers its router in `create_app` with one line.
- `GET /api/health` → `{"ok": true}`; the only unauthenticated route.
- WebSocket routers (`/ws/*`) are registered without `require_session`; each handler calls `check_ws` before `accept()`, and when it fails calls `await ws.close()` without accepting and returns, which uvicorn answers with HTTP 403 (returning without any handshake gives 500).
- The PTY child environment is built by a pure function that drops every `FENRIR_*` var (plan 13). In the Docker image the PTY runs as the user named by `FENRIR_PTY_USER` (`claude`, set by the image alone), dropping privileges with `subprocess`'s `user=`, `group=`, `extra_groups=[]`. Isolation check (DEC-1): pure `fenrir.server.isolation.check(euid, pty_user_uid, pty_user_gid, private_dir_stat, private_dir_path, private_dir_real, app_roots, env_file_path=None, env_file_real=None) -> list[str]` (reasons; empty = isolated; `None` uid/gid = no such user, `None` stat = no dir) and shell `isolation_problems(settings) -> list[str]`, which is `[]` unless `FENRIR_BIND_ALL=1` and otherwise checks this process: euid 0; `FENRIR_PTY_USER` names an existing user with uid ≠ 0 and primary gid ≠ 0; `private_dir` is judged as configured (`absolute()`, `lstat`, never following a symlink): its path holds no symlink or `..` (`absolute() == resolve()`), neither it nor its resolved target is under `/app` or the cwd, and it is a directory, root-owned, mode 0700; a set `FENRIR_ENV_FILE` (absolute) holds no symlink or `..` and is inside `private_dir`. `fenrir serve` refuses to start (exit 1, before binding) when it would bind all interfaces and there is any problem, and otherwise logs each at start; the terminal and every strategy child call it and refuse (logging why) when it is non-empty. Natively, with `FENRIR_PTY_USER` unset, the PTY runs as the server's own user.
- `WS /ws/terminal` (Plan 13): refused before `accept()` (HTTP 403) on a failed `check_ws`, or in the image when `isolation_problems` is non-empty (reasons logged). Each connection gets its own shell in a PTY (80×24 until the first resize) in the server's cwd (the repo), with `terminal.child_env(os.environ, session_key)` and, when `FENRIR_PTY_USER` is set, its passwd `HOME`, `USER`, `LOGNAME`, `SHELL` (so `shell_argv` prefers its own shell). Client → server JSON text frames `{"type":"input","data":string}` (encoded UTF-8 with `errors="replace"`) and `{"type":"resize","cols":int,"rows":int}` (clamped to 1–1000); anything else, binary frames included, is ignored. Server → client: binary frames of raw PTY output. Input waits in a buffer until the shell reads it, so a disconnect is seen even mid-paste; the socket closes with 1009 when one input frame is over 1 MiB or more than 1 MiB queues behind a write the shell is not reading (so at most about 2 MiB is held: that queue plus the write in flight). A `FENRIR_PTY_USER` naming no user is refused before `accept()` (HTTP 403, logged). When the shell exits the socket closes with 1000; when it cannot start (no such shell, or taking the PTY as controlling tty fails) the server sends one output line `cannot start the shell <path>: <reason>` and closes with 1011. On disconnect the server hangs up the PTY, gives the shell 1 s to pass SIGHUP to its jobs, then SIGKILLs its process group and every live process still in its session (session id = the shell's pid), listing and killing again until the session is empty (at most 10 rounds, logged if any remain; a process it may not kill, such as a setuid program, is skipped and logged by pid) so a job forking mid-kill is caught too; background, `nohup` and `disown` jobs die under any shell. The teardown runs to the end even if the handler is cancelled. Only a process that calls `setsid` leaves the session and outlives the disconnect, running as the PTY user with the same scrubbed environment; it is not tracked or killed.
- Strategy code never runs as root (DEC-1): when `FENRIR_PTY_USER` is set, every `load_strategy` and `step` the server needs (the strategy list, backtest jobs, the live runner) runs in a child process as that user with an environment holding no `FENRIR_*` secrets, exchanging JSON over a pipe; natively it runs in the server's own processes. With `FENRIR_BIND_ALL=1` (the image), the strategy list, backtest jobs and the runner refuse to load any strategy when the isolation check fails, exactly as the terminal does (`jobs_runner.strategy_user(settings)` raises ValueError with `isolation_problems` reasons; the job's items error with it). The server itself runs from the image (`/opt/fenrir`), never from `/app`.
- Terminal-writable files read as root (DEC-1, #140): the repo (`/app`) and `data/` are writable by the terminal user, so the root server and the root pull read every file there through `fenrir.files.read_regular(path, limit) -> bytes`: opened `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, `fstat` must say regular file, at most `limit` bytes read, the fd closed exactly once. A symlink at the path, a FIFO, socket, device or directory, or a file over `limit` is `ValueError` naming the file (`<path> is a symlink; not reading it` / `is not a regular file` / `is over <limit> bytes`), never quoting its contents; a missing file stays `FileNotFoundError` and a denied one `PermissionError`. Readers and limits: `data/results/*.json` (`cache._read`, so `list_reports`, `read_report`, `cached_run`; 16 MiB), `data/jobs/*.json` (`JobRunner.start`, skipped and logged; 16 MiB), `data/accounts.yaml` (1 MiB), `data/bars/<series>/1m/*.parquet` (`load_bars`, `coverage`; the bytes, not the path, go to polars; 256 MiB each), `bars/manifest.json` (its own dir-fd walk, 16 MB), `rules/<firm>/<account>.yaml` (`load_account`, `write_account`'s read; 1 MiB), `instruments.yaml` (`load_instruments`; 1 MiB), strategy files hashed by `describe` (1 MiB) and the repo `.env` probe. Limit: only the last path component is never followed (the manifest and `data/jobs` excepted); a symlinked directory on the way (`data/results`, `bars/<series>/1m`, `rules/<firm>`) is still followed, so a regular file with the reader's extension in the link's target can be read, and a rules YAML error can quote such a file.
- `Settings` gains `private_dir: Path` (`FENRIR_PRIVATE_DIR`, default `data/private`; `/var/lib/fenrir` in the image): `session.key` and `fenrir.db` live there. The secrets file `load_settings` reads is `$FENRIR_ENV_FILE` (default `.env`; `/var/lib/fenrir/.env` in the image); with `FENRIR_BIND_ALL=1` it never falls back to the repo `.env`, so with `FENRIR_ENV_FILE` unset it reads no file. Children the server spawns call `load_settings` under the same environment, so the same holds for them. `FENRIR_ENV_FILE`, `FENRIR_PTY_USER` and `FENRIR_BIND_ALL` are read from the process environment only, never from a file.

### Plan 12 — web

- `web/src/api.ts`: `apiFetch<T>(path: string, init?: RequestInit): Promise<T>` (same-origin request, so the session cookie rides along; throws `ApiError` with status + body text on non-2xx), `wsUrl(path: string): string` (same host, `ws:`/`wss:`). A 401 shows "Session expired: restart the server (`docker compose restart`) and open the new launch link". `ApiError` has `status: number` and `body: string`. `web/src/errorText.ts` exports `errorText(e: unknown): string` (plans 14–16).
- Routes: `web/src/routes/<page>.tsx`, each exports `<page>Route` created with `createRoute` and imports `import { rootRoute } from './root'`; `web/src/router.tsx` assembles the tree. Pages: `/` Workbench, `/rules`, `/data`, `/backtests`, `/backtests/$key`, `/strategies`, `/live`, `/settings`.
- Query keys: `['<area>', ...args]`, e.g. `['backtests', 'results']`.
- Styling: implements root `DESIGN.md` exactly. Dark only: DESIGN.md's token block sits in `:root` of `web/src/index.css` (no `.dark` class, no light palette, `@custom-variant dark` always matches); radius 0; no shadows; colour only for abnormal or selected state (Steel normal, Ember caution/selected, Red halt/breach/destructive; never green). Tailwind colours: `ground`, `ground-deep`, `rule`, `rule-strong`, `idle`, `ink`, `ink-2`, `ink-3`, `ember`, `red` plus shadcn's variables. Utilities in `index.css`: `label` (11px tracked mono caps, Slate), `value` (13px mono figure), `step` (90ms `steps(2)` transition). Base `h1` = page title, `h2`/`h3` = label. Fonts: `font-sans`/`font-mono` = IBM Plex Mono, `font-display` = Chakra Petch, imported from `@fontsource` in `main.tsx`. Canvas widgets (xterm, lightweight-charts) read colours from the CSS variables via `getComputedStyle`. No hand-written CSS classes outside `index.css`; no inline styles except runtime-computed values.
- Shell: `rootRoute` renders the index rail (plain `<nav>`, not shadcn Sidebar; active page = boxed cell with an Ember block) and `<main>`. Every page renders `<Page title="…" strip={…}>` from `web/src/components/page.tsx` (top strip with the `h1`, optional strip content such as the kill switch, then the body; `className` overrides body padding). `web/src/components/state-text.tsx` exports `StateText({ tone: 'normal' | 'idle' | 'caution' | 'alarm', children })` for one-word statuses (replaces Badge).
- Components: import as `@/components/ui/<name>`, `cn()` from `@/lib/utils`. Each plan adds the components it first needs with `cd web && npx shadcn@latest add <names>` (answer No if one already exists). `Button` variants are replaced (plan 12) by DESIGN.md's control: `outline` (default, plain), `destructive` (red edge block: kill, resolve, destructive confirms), `caution` (ember edge block: arm live); sizes `default`, `sm`, `icon`; no filled variant. Confirmations (arming live, disarm, resolve, kill switch) use `AlertDialog` (destructive confirm: `className={buttonVariants({ variant: 'destructive' })}`); tables `Table`; view switches `Tabs`; forms `Input`/`Label`/`Checkbox`/`Select`/`Switch`. No `Badge`, `Card` or `Progress`: statuses are `StateText` or a code strip, panels are hairline-divided sections or sleeves, meters are segmented. The rules form does not use shadcn's `Form` (react-hook-form/zod): it keeps the pure `setIn` state and server-side validation.
- Dev: `npm --prefix web run dev` proxies `/api`, `/ws` and `/launch` to `127.0.0.1:8000`; open the `:5173` launch link `fenrir serve --dev` prints.
- Playwright tests in `web/e2e/`, run against `fenrir serve` with fixture data dir `tests/fixtures/e2e/`.

### Plan 14 — backtest jobs API

- `GET /api/backtests/options` → `{strategies: string[], accounts: string[], contracts: string[]}`: `strategies` are the `<strategies_dir>/*.py` paths, relative to the cwd when under it (e.g. `strategies/orb.py`), else absolute, glob-escaped (`glob.escape`) so each is ready to POST and matches only its own file; file names only with no `MARKET` (reading it would execute the file); `accounts` from `list_accounts`; `contracts` every symbol in `instruments.yaml`; `500 {detail: "<strategies_dir> is a symlink; not reading it"}` with no listing when `strategies_dir` itself is a symlink (the same `fenrir.files.refuse_symlink` check as `GET /api/strategies`), and `500 {detail}` naming the file when `instruments.yaml` is a symlink, not a regular file or too big.
- `POST /api/backtests/jobs` `{strategies: string[], accounts: string[], contracts: string[] | null, include_holdout?: boolean (default true)}` → `{job_id}`. Each strategy glob (relative to the cwd, or absolute) must hold no `..` part and resolve (symlinks followed) inside `strategies_dir`, checked before `expand` runs anything, else 422 `{detail}`; that check cannot see a symlink behind a wildcard part, so the `--expand` child then resolves every match (symlinks followed) and, before loading any file, refuses the whole grid (422 `{detail: "ValueError: '<match>' is not a file inside <strategies_dir>"}`) if one is not a regular file inside `strategies_dir`; `expand`'s ValueError (a glob matching nothing, a contract fitting no strategy, a timeout) is also 422 `{detail: <its message>}`; a malformed body is FastAPI's 422; 409 `{detail: "a grid is already being expanded"}` while another POST's `expand` runs (one at a time).
- `GET /api/backtests/jobs` → `Job[]`; `GET /api/backtests/jobs/{id}` → `Job` where `Job = {id, created_at, include_holdout, items: {strategy, account, contract, status: "pending"|"running"|"done"|"error", key: string|null, error: string|null, progress: [done, total]}[]}`. Jobs persist in `data/jobs/<id>.json`; on server start, unfinished items re-run; a job file that is corrupt, a symlink, not a regular file or over 16 MiB is skipped and logged, never followed. `app.state.jobs` is a `JobRunner` (`fenrir.server.jobs_runner`: `JobRunner(settings, *, secret=session_key)`, `start()`, `stop()`, `expand(strategies, accounts, contracts) -> tuple[GridItem, ...]` (ValueError → 422), `submit(grid, include_holdout) -> Job`, `jobs() -> list[Job]` newest first, `get(id) -> Job | None`, `add_listener(fn)`/`remove_listener(fn)` with `fn(to_json(job))` called from the runner thread on every change); `fenrir serve` calls `app.state.jobs.start()` and `stop()` on shutdown (`stop()` kills the running child; its items re-run on the next start). Each job's unfinished items run in one child, `python -P -m fenrir.server.job_child` (`-P`: the cwd, which the terminal can write, is not on `sys.path`) (stdin `{include_holdout, items: [[index, strategy, account, contract]]}`; stdout one JSON event per line: `{i, status: "running"}`, `{i, progress: [done, total]}`, `{i, status: "done", key}`, `{i, status: "error", error}`), started in its own session, with `terminal.child_env(env, session_key)` plus the data, rules and strategies dirs and hold-out months, and as `FENRIR_PTY_USER` when set (`HOME`, `USER`, `LOGNAME`, `SHELL` from its passwd entry; Plan 11 DEC-1). An item's error is `"<ExceptionType>: <message>"`, at most 2 KB; child lines over 64 KiB are dropped; a child silent for 30 min is killed and its unfinished items error `no progress for 30 min`. The runner refuses (logs, errors the items, writes nothing) while `data_dir` or `data/jobs` is a symlink; job files are mode 0644. **Strategy files are never executed in the server (DEC-1):** `expand_grid` loads each strategy to read its `MARKET`, so the API calls `runner.expand(...)` (the same child with `--expand`: stdin `{strategies, accounts, contracts}`, one stdout line `[[strategy, account, contract], ...]` or `{error}`, killed after 60 s; each row must be inside `strategies_dir` with a known account and contract) and then `runner.submit(...)`. The job child re-checks each item's strategy the same way (resolves to a regular file inside `strategies_dir`) before running it, else that item errors `ValueError: '<path>' is not a file inside <strategies_dir>` (job files are read back from `data/jobs`). "Inside" means inside `strategies_dir` as resolved when the child starts. Limits: a hardlink or copy placed inside counts as inside; the checks and the load are not atomic, but both run as the strategy user, who could run the file anyway. `/options`' strategy list, if it reads `MARKET` or anything else by loading a file, must also come from an isolated child (#44, #92).
- `GET /api/backtests/jobs/{id}` → 404 for an unknown id. `fenrir serve` starts and stops the runner through the app's FastAPI lifespan, with a 5 s graceful-shutdown limit so `stop()` runs promptly; `stop()` also kills any running expand child.
- `WS /ws/jobs`: on connect sends every current `Job` (one message each, newest first), then a `Job` on every change; a slow client gets only the latest state per job id (stale frames are dropped); the client upserts by `id`. The client sends nothing; the server reads only to notice the close.
- `GET /api/backtests/results` → `Report[]` newest first (without `sample_equity`); `GET /api/backtests/results/{key}` → full `Report`, 404 for a key that is not 64 hex chars or has no file. In these and `/compare`, every period also carries `expected_net_per_attempt` (`fenrir.backtest.compare.with_net`, from `data/accounts.yaml` read per request). A corrupt cached report (not JSON, or missing `created_at`, `key`, `strategy_hash`, `account` or `periods`, or a period that is not an object or null), a report file that is a symlink, not a regular file or over 16 MiB, or a broken `accounts.yaml` is 500 `{detail}` naming the file.
- `GET /api/backtests/compare?view=account|strategy` → `[{group, rows: Report[]}]` = `compare(latest_only(list_reports(settings), choices), view, choices)` (`fenrir.backtest.compare`, plan 10), ranked by tuning results only: `p_pass_and_payout`, then net per attempt. `view` is required; anything else is 422.

### Plan 15 — rules API

- `GET /api/rules` → `[{id, verified_on, age_days, stale, error}]` (stale = null, future or > 30 days; `error` names the file relative to the rules dir, e.g. `lucid/flex-50k.yaml: …`, or `lucid/flex-50k.yaml is a symlink; not reading it` / `is not a regular file` / `is over 1048576 bytes`, which `PUT` also refuses (422) before writing anything); `GET /api/rules/{id}` → that row plus `rules: object | null` (`AccountRules.model_dump(mode="json")`, null when the file does not validate); `PUT /api/rules/{id}` body = the `rules` object → 200 row | 404 | 422 `{detail, errors: [{loc: (string|number)[], msg}]}` (pydantic `loc`; a rename is an error at `["account"]`; atomic write via `fenrir.rules.writer.write_account(path, rules)`, a `ruamel.yaml` round-trip (`preserve_quotes=True`) that writes only values that changed by value (`2100 == 2100.0` is unchanged), keeps comments and formatting, and validates the result with `load_account`'s parser (`parse_account`) before the atomic replace; saving an unchanged object leaves the file byte-identical (DEC-3)). The UI lists `GET /api/rules` under query key `['rules']`; each row's `Edit <id>` link goes to `/rules?account=<id>`, which selects the file the editor opens. The editor is a field-by-field form (`web/src/rulesForm.ts`, pure `setIn`). e2e runs serve `FENRIR_RULES_DIR=tests/fixtures/e2e/rules`, a fresh gitignored copy of `rules/`.

### Plan 16 — data API

- `GET /api/data` → `{series: Coverage[], rolls: Roll[], front: {ES, NQ}, pull}`, or 500 `{detail}` naming the file when a `*.parquet` under `bars/` or `instruments.yaml` is a symlink, not a regular file or too big (never followed or read, so a FIFO never holds a thread); `POST /api/data/pull` → 202, or 409 while a pull runs (one `fenrir data pull` child process at a time).
  - `Coverage = {series, first_day: string|null, last_day: string|null, days: number, gaps: [string, string][]}` (dates `YYYY-MM-DD`; the same objects as `fenrir data summary --json`'s `series`; an empty data dir gives nulls, 0 and `[]`); `Roll = {series, start, contract}` for every roll in `instruments.yaml`; `front = {ES: string|null, NQ: string|null}` for today's trading day, null outside the rollover calendar; `pull = {state: "idle"|"running"|"done"|"error", started_at: string|null, finished_at: string|null, returncode: number|null, log_tail: string[]}` (UTC ISO with `Z`).
  - `POST /api/data/pull` → 202 with an empty body (refetch `GET /api/data` for status), `409 {detail}` while running, `500 {detail: "refusing to pull: <isolation reasons>"}` (nothing started) when `FENRIR_BIND_ALL=1` and the DEC-1 isolation check fails; 401 without the session cookie, 403 cross-origin.
  - The pull child (DEC-1, #135): it runs as the server's user (root in the image), never as `FENRIR_PTY_USER`: a process owned by the terminal's user exposes its env (the LSE key) through `/proc/<pid>/environ`. Its env (`pull.pull_env(environ, settings)`) is the server's minus `FENRIR_NT_TOKEN` and `FENRIR_ENV_FILE`, plus `FENRIR_LSE_API_KEY` and `FENRIR_DATA_DIR` from the server's settings, so the LSE key is the only secret it gets. `data/` is terminal-writable in the image, so the store's writes (`write_contract`, `write_manifest`, `write_bars`) walk a chain of directory fds opened `O_DIRECTORY|O_NOFOLLOW` from `data_dir`, write a random-named `O_EXCL|O_NOFOLLOW` temp file (mode 0644) and `os.replace` it over the target: a symlink on the way, even one swapped in mid-write, raises `ValueError` and writes nothing; a symlinked target is replaced, never followed. `read_manifest` and `data_version` refuse a symlinked `bars/` or `manifest.json`, or a non-regular or over-16 MB manifest, with `ValueError` (a missing one reads as empty). `FENRIR_DATA_DIR` must be `data` under the server's cwd or a path whose ancestors are root-owned: only `data_dir` itself is opened `O_NOFOLLOW`, so a terminal-writable ancestor could be swapped for a link.

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

Instrument names are NinjaTrader's (`"NQ 12-26"`). Wrong token → HTTP 401 / WS close. `client_id` is at most 24 characters; `side` ∈ `"long"|"short"`. The `{type: "account"}` message repeats every 2 s per managed account.

Add-on behaviour (DEC-5): its config file lists `managed_accounts`; a request naming any other account → `{accepted: false, error: "unmanaged_account"}` and its `client_id` is not recorded as seen; `/accounts`, the 16:40 flatten and `/flatten-all` cover managed accounts only; an empty list is logged loudly at start. Seen `client_id`s persist to a file beside the config (pruned to 7 days) and are loaded at start. `flatten_on_disconnect_seconds` (default null = off) flattens managed accounts holding a position after no `/stream` client for that long. On every entry fill the add-on sets the OCO stop and target quantity to the total filled so far (placed on the first fill, resized on later ones); an entry not complete after 2 s has its remainder cancelled, and fills arriving after the cancel are still protected. Entry is a market order; stop and target are OCO. `/orders` returns working orders only; `role` adds `"other"` for orders not placed by Fenrir. Fill `qty` is signed (+ buy, − sell); `/orders` `qty` is unsigned, the remaining quantity (> 0), with the side in `action`. `ts` is UTC ISO 8601 with milliseconds and `Z`. `/flatten-all` flattens and cancels on every managed account.

```python
# fenrir.bridge.names
def nt_instrument(symbol: str, front_code: str) -> str   # ("MNQ", "NQZ26") -> "MNQ 12-26"
# fenrir.bridge.client  (pure parsing + async shell; httpx + websockets)
class Ack: accepted: bool; duplicate: bool; error: str | None   # duplicate=True: an earlier attempt landed; treat as success
class AddonClient(base_url, token, *, timeout=5.0, retries=3, heartbeat_timeout=6.0, reconnect_delay=1.0):
    accounts() -> list[Account]; positions(account) -> list[Position]; orders(account) -> list[Order]
    bracket(client_id, account, instrument, side, qty, stop, target), flatten(client_id, account),
    cancel(client_id, account, order_id), flatten_all(client_id) -> Ack; stream() -> AsyncIterator[dict]; aclose()
    # Account/Position/Order are TypedDicts in the /accounts, /positions, /orders row shapes (plain dicts; keys match
    # reconcile's NtPosition/NtOrder fields). Every response and stream message is validated strictly (no "1" -> 1,
    # no NaN, ts must be ...HH:MM:SS.mmmZ); malformed -> ValueError naming the first bad field and error kind,
    # input not echoed.
    # HTTP errors and an unreachable add-on raise httpx.HTTPError; a 401 is never retried. POSTs retry only on a
    # transport error (timeouts included) with the same body, so a retry after a landed order comes back duplicate.
    # stream() never ends: yields {"type":"connected"} after each (re)connect, then validated events, and
    # {"type":"disconnected","error":str} on a drop, bad token, malformed message or no message at all for
    # heartbeat_timeout (default 6 s = 3 missed heartbeats; at most ~1 s more to give up on a half-open peer);
    # reconnects with backoff doubling to 30 s, reset only by a valid message.
    # The token is never logged or put in an error. Env proxies (HTTP_PROXY etc.) are ignored: the add-on is always
    # a direct peer. Plan 22 sets app.state.addon: AddonClient.
# fenrir.bridge.fake_addon  (test support, shared with live tests)
class FakeAddon: token, accounts, positions, orders, requests, seen, delays, heartbeats, heartbeat_every; push(msg), kick()
async def running(fake) -> AsyncContextManager[str]   # serves make_app(fake) on a free loopback port, yields base URL
def now_ts() -> str                                   # contract ts shape, e.g. "2026-10-12T13:31:00.250Z"
# fenrir.bridge.reconcile
class NtPosition: account: str; instrument: str; qty: int; avg_price: float          # /positions row, qty signed
class NtOrder: account: str; order_id: str; client_id: str; instrument: str; role: "entry"|"stop"|"target"|"other"; action: "buy"|"sell"; qty: int; price: float; state: str   # /orders row
class ExpectedBook: account: str; instrument: str; qty: int                          # signed qty the runner believes it holds
class Mismatch: kind: MismatchKind; detail: str                                        # all frozen dataclasses
def reconcile(expected: ExpectedBook, positions: Sequence[NtPosition], orders: Sequence[NtOrder]) -> tuple[Mismatch, ...]
    # MismatchKind = Literal["position", "unexpected_position", "missing_stop", "missing_target", "stray_orders"]
    # () when the book matches; at most one of each kind, in that order (several unexpected instruments are
    # listed in one unexpected_position). Rows for other accounts are ignored;
    # orders are working orders. While a position is held, only stop/target orders on that instrument whose
    # action closes it and whose qty > 0 count as protection, and their qty must equal |held| (fewer or more → missing_stop /
    # missing_target). Every other working order on the account (a stale entry, "other", a wrong-direction
    # or other-instrument stop, a qty ≤ 0, any order while flat) → stray_orders, naming its order_ids.
    # Strays are for the user to resolve: plan 22 must not auto-cancel them while an unexpected_position
    # exists, since a "stray" stop may be the only protection on that position.
```

### Plans 21–24 — live API and storage

SQLite at `<private_dir>/fenrir.db` (DEC-1), owned by `fenrir.live.journal` (plan 21): tables `journal(id, ts, account, kind, payload_json)` with `kind ∈ {"bar","action","rejection","fill","alert","halt","peak"}` (`peak` added by plan 22.2: payload `{peak, ts}`, written each time the runner raises an account's peak; on start the runner uses the highest of `armed.peak_balance` and the journalled peaks, and halts with "confirm balances" when an `end_of_day` session close passed while it was down), and `armed(account PRIMARY KEY, strategy_path, strategy_hash, rules_account, contract, mode, phase, armed_at)` with `phase ∈ {"evaluation","funded"}`; plan 23.3 adds `dll_on`, `rules_hash`, `instruments_hash`, `start_balance`, `peak_balance` with a guarded migration (DEC-4a/d). The runner halts an account whose strategy, rules or `instruments.yaml` hash differs from `armed`, or whose `dll_on` differs from `accounts.yaml`. `fenrir.live.journal.open_db(path: Path) -> sqlite3.Connection` creates both tables if missing. `src/fenrir/live/arming.py` is created by plan 21 (`Armed`, `list_armed`, `insert_armed`, `delete_armed`); plan 23 adds `ArmRequest`, `Refusal`, `decide` to that file. The runner re-reads `armed` on every closed bar and halts an account whose strategy file hash ≠ `armed.strategy_hash`.

Runner core (plan 21.2), `fenrir.live.core`, pure; the shell journals each `Log` with `journal.write`:

```python
@dataclass(frozen=True, slots=True)
class Start: phase_start: float; peak: float; balance: float   # NinjaTrader balances, absolute $ (DEC-4a)
@dataclass(frozen=True, slots=True)
class DayStart: balance: float; peak: float   # NinjaTrader balance and drawdown peak (as Start.peak) at today's session open, absolute $
@dataclass(frozen=True, slots=True)
class Setup: armed: Armed; strategy_hash: str; rules: AccountRules; spec: ContractSpec; safety_margin_pct: float; start: Start | None = None; day_start: DayStart | None = None
    # strategy_hash = file_hash on disk now; rules = with_dll applied; start and day_start ignored for dry runs (they start fresh)
    # day_start supplied by 21.5 (warm-up) or 22.2 (NinjaTrader figures): both the open's balance and its peak
@dataclass(frozen=True, slots=True)
class Log: kind: str; payload: Mapping[str, Any]     # kind ∈ journal KINDS
def start(setup, now) -> (RunnerState, effects)      # halts (open-ended) on a strategy hash mismatch, or sim/live without start;
    # such an account cannot start: account_status gives balance, today_pnl, limits, position, session = null
def start_account(rules, phase, day, start: Start | None, day_start: DayStart | None = None) -> AccountState   # relative to phase start; floor trails start.peak like drawdown's trail
    # (intraday: max(peak, balance)); day_start_balance = day_start.balance − phase_start (start.balance when None)
def on_bar(s, bar) -> (RunnerState, effects)         # appends to the ≤500-bar window and today's equity, then the safety check
def ctx(s) -> Ctx                                     # what step sees; step runs in a child (DEC-1), never in the core
def decide(s, actions: Sequence[Action]) -> (RunnerState, effects)   # "bar" {**bar, actions: n}, then per action
    # "action" {type, **fields, mode, sent: false} or "rejection" {action: {type, **fields}, reason}
def step_failed(s, error: str) -> (RunnerState, effects)   # "bar" (actions 0) + open-ended "halt"
def halt(s, reason) -> (RunnerState, effects)              # open-ended; "halt" {reason, kind, until: ISO day | null}
def safety_level(s) -> float                               # session.safety (DEC-4c)
def account_status(s) -> dict                              # one AccountStatus
```

A Bracket is rejected when halted, for sim/live while `day_start` is None, after another Bracket accepted on the same bar, or when a later Flatten/Cancel on the same bar replaces it (journalled as a rejection: the backtest never places it), while in a position, when the bar closes at or after `min(session.flat_by, "16:40")`, or when `fenrir.rules.pretrade.check` rejects it at the bar's close (including a qty that is not a positive whole number, e.g. NaN or 1.5 from the child's JSON); Flatten and Cancel always pass. Apart from the `day_start` block (live only), these are the backtest's decisions. The safety margin day-halts (`halt_kind` "safety", until the current trading day) when close-mark equity is at or below `safety_level`. For sim and live, `day_start` sets today's opening balance (`day_start_balance` and `session.start`), which drives the daily loss room and the funded scaling tier, and the floor at the open (from `day_start.peak`, exact for `end_of_day` and `intraday` trailing and `locks_at`), which with it fixes the safety level's room; while it is None every Bracket is rejected with "today's opening balance unknown" (Flatten and Cancel pass), so a mid-day restart never trades on the balance at restart. 21.5 or 22.2 must supply both figures; one without the other is not enough.

- `GET /api/live/status` → `{lse: {connected, last_bar_ts}, nt: {connected, last_heartbeat}, drift: {ok: boolean, ticks: number|null}, accounts: AccountStatus[]}`, `AccountStatus = {account, mode: "dry"|"sim"|"live", phase: "evaluation"|"funded", strategy, rules_account, contract, halted: boolean, halt_kind: "safety"|"reconcile"|"stale"|"drift"|"breach"|null, halt_reason: string|null, balance, today_pnl, limits: {drawdown_remaining, drawdown_max, daily_loss_remaining: number|null, daily_loss_max: number|null, max_minis, held_minis}, position: {qty, entry, stop, target}, session: {start: number (equity at session start, relative to phase start), floor: number, target: number|null, safety: number (equity level of today's safety margin, recomputed each bar: the higher of start − margin × daily loss limit (when on) and floor + (1 − margin) × (start − floor at session start); DEC-4c), equity: [ts, number][] (one point per closed bar this trading day, close mark), fills: string[] (ISO ts of today's fills)}}`.
  - `halted` is true for any halt and for a stale or drift pause (the brief `unreconciled` pause while connecting to NinjaTrader appears only in `halt_reason`); `halt_kind` names it (`safety` margin or daily loss limit day halt, `reconcile` mismatch, `stale` feed pause, `drift` LSE-vs-NinjaTrader pause, `breach`), and is null for other halts (strategy error, strategy file changed, mode mismatch, cannot start). Plan 21 sets `safety`, `stale`, `breach`; plan 22 sets `reconcile`, `drift` and fills `drift` (`ticks` = largest last entry-fill distance from the LSE close; plan 21 reports `{ok: true, ticks: null}`).
  - An account that cannot start has `balance`, `today_pnl`, `limits`, `position` and `session` = null; `halt_reason` says why.
- Code strips (plan 24, DESIGN.md): the **system strip** is Feed (alarm when `lse.connected` is false, caution when any account has `halt_kind == "stale"`), Broker (alarm when `nt.connected` is false — the add-on stream reports a missed heartbeat as disconnected; idle while every account is a dry run), Drift (caution when `drift.ok` is false). **Each account strip** is Risk (caution when `halt_kind == "safety"` or equity `session.start + today_pnl` is at or below `session.safety`; alarm when `halt_kind == "breach"`) and Book (alarm when `halt_kind == "reconcile"`). Stale and drift pauses light the system cell, not the account. Beside the system strip, the page names the most severe account needing action as `<cell> · <account>`, e.g. "Book · LFE 0911" (other halts use "Halt").
- `GET /api/live/journal?account=ID&day=YYYY-MM-DD` → `JournalRow[]` (`{id, ts, account, kind, payload}`).
- `POST /api/live/kill` → `{ok, error}`: flatten all, cancel all, disarm all (plan 22).
- `POST /api/live/resolve` `{account}` → `{ok}` | 404 when the account is not running: clears a reconciliation halt after the user fixed it (plan 22).
- `GET /api/strategies` → `[{name, path, hash, market, params, error, reports: Report[] (without sample_equity, matching current hash, periods with `expected_net_per_attempt`)}]` (plan 23), one per `strategies/*.py`, sorted by path; a file that fails to load has `error` set, `market: null`, `params: {}`, `reports: []`, and `hash: null` when the file was not read (unreadable, isolation refusal, loader crash or timeout). Reports match on `strategy_hash` only and are newest first; `500 {detail}` for a broken `accounts.yaml` or cached report; `409 {detail: "the strategies are already being loaded"}` while another GET's `describe` runs (one at a time); `500 {detail: "<strategies_dir> is a symlink; not reading it"}` when `strategies_dir` itself is a symlink (the terminal can swap it; `describe` also raises ValueError then, so root never globs or hashes another directory). **Loading happens in an isolated child (DEC-1):** the server lists file names only and calls `app.state.jobs.describe(paths)` (`JobRunner`, Plan 14), which spawns `python -P -m fenrir.server.job_child --describe` exactly as the job child (own session, `terminal.child_env`, as `FENRIR_PTY_USER` when set; refused while `isolation_problems` is non-empty); stdin `{paths: string[]}` (each directly in `strategies_dir`, else ValueError before spawning; only the files the server first read as regular files of at most 1 MiB, opened without following a symlink or blocking on a FIFO, read with `fenrir.files.read_regular`, else that file's `error` is `ValueError: <path> is a symlink; not reading it` / `is not a regular file` / `is over 1048576 bytes`); stdout one line per file in order, `{path, name, hash, market, params, error}` with `hash` the sha256 of the bytes loaded and `error` `"<ExceptionType>: <message>"`. Child output is untrusted (`jobs_runner.parse_description`): `path` must be one requested and `name` its stem, `hash` 64 lowercase hex (null only with `error`), `market` `ES`/`NQ` with `params` an object and `error` null, or `market` null with `params` `{}` and `error` a string (clipped to 2 KB); NaN/Infinity, lines over 64 KiB and other lines are dropped. A file left undescribed gets `error` = the spawn error, `loading the strategies took over 30 s` (the child is killed when silent that long) or `the strategy loader stopped before describing this file`. Strategy code can write to the child's protocol pipe, so the server checks each line against that read (never executing the bytes): a file described more than once gets `the strategy loader described this file more than once`, and one whose `hash` is not the bytes the server read gets `the file changed while loading`, both with `hash: null`. Limit: a forged line with the right hash still stands in for a file the child never reached (an earlier file ended the child, and shows `the strategy loader stopped before describing this file`); its `hash` and `reports` are then the file's own, but its `market` and `params` are the forger's.
- `GET /api/arming` → `Armed[]`; `GET /api/arming/accounts` → NinjaTrader accounts `[{id, name, is_sim, ...}]` | `502 {error: "nt_unavailable"}`; `POST /api/arming/arm` `{account, strategy_path, rules_account, contract, mode, phase, confirm: boolean, start_balance?: number, peak_balance?: number}` (NinjaTrader balances at phase start and the peak so far: highest end-of-day balance for `end_of_day` trailing, highest intraday equity for `intraday`; required for sim and live (422 when missing or `peak_balance < start_balance`), ignored for dry runs; DEC-4a) → `200 Armed` | `409 {error: "no_backtest"|"hash_changed"|"rules_changed"|"needs_confirmation"|"already_armed"|"unknown_account"|"mode_mismatch"|"no_evaluation_phase"|"dll_mismatch", summary?: PeriodResult & {expected_net_per_attempt}}` | `400` (strategy path outside `strategies/` or unknown `rules_account`) | `502 {error: "nt_unavailable"}`. A matching backtest has the same `strategy_hash`, `rules_hash` (else `rules_changed`, plan 23.3), `account` (= `rules_account`) and `contract`, and its `dll_on` equals the account's current `dll_on` in `<data_dir>/accounts.yaml` (else `dll_mismatch`: re-run the backtest); the live confirmation summary also carries `dll_on`, `rules_hash` and the computed liquidation level; `POST /api/arming/disarm` `{account}` (plan 23). No CLI command arms or disarms.
- `GET /api/settings/accounts` → `{[account_id]: {eval_price, dll_on}}` for every account in `rules/` (defaults filled in); `PUT /api/settings/accounts` partial map → merged into `<data_dir>/accounts.yaml` (user data, not `.env`, not `rules/`); 422 on a bad value, unknown key or unknown account, writing nothing (plan 24); responds like `GET`; a broken `accounts.yaml` is 500 `{detail}` and is never overwritten; the file is written mode 0644 (job children read it). Prices apply at once; `dll_on` applies to new backtests and to arming (a matching backtest needs it), and an armed account whose saved `dll_on` differs from the one pinned at arming halts until re-armed (DEC-4d, plan 23.3).
- `GET /api/settings` → settings with secrets masked (`"••••1234"`): `{lse_api_key, nt_url, nt_token, holdout_months, safety_margin_pct, stale_seconds, repo_env_secrets}` (the path settings are not editable here; a bad value in the file is 500 `{detail}` naming it); `PUT /api/settings` partial update of those six keys (422 `{detail, errors: [{loc, msg}]}` for an unknown key, a wrong type, `holdout_months < 0`, `safety_margin_pct` outside (0, 100], `stale_seconds <= 0`, an `nt_url` that is not `http(s)://`, a line break, or a secret starting `••••` that is not its current mask; nothing is written on 422; a missing file is created, mode 0600; responds like `GET`) writes the secrets file (`$FENRIR_ENV_FILE`) in place (comments and unknown lines preserved); a secret field sent back equal to its mask, or absent, is not changed; changes apply on restart; `GET` reads the file fresh (plan 24). The image never passes secrets via `env_file` or environment, so edits are not shadowed by os.environ. In the image, `GET /api/settings` also reports `repo_env_secrets: boolean` (always present; `false` natively, where the repo `.env` is the secrets file) (a repo `/app/.env` holds secret keys) so the page can warn and offer to import them into the secrets file. `POST /api/settings/import-repo-secrets` (no body; session + Origin like `PUT`) copies the non-empty `FENRIR_LSE_API_KEY` and `FENRIR_NT_TOKEN` from the repo `/app/.env` (read as for `repo_env_secrets`: image only, `read_regular`, anything odd = none) into the secrets file, only where that key is empty there (a secret already set is never replaced), through `PUT`'s checks and write (422 `{detail, errors}` writing nothing, e.g. a value starting `••••`); no other key is copied, the repo `.env` is left as it is, and the response is like `GET` (secrets masked, never in full) plus `imported: string[]` (the field names written, e.g. `["nt_token"]`); `409 {detail}` when nothing is left to import: it holds no secrets (always natively) or they are all already set.
