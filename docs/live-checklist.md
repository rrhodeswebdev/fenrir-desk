# Live runner checklist: dry run → sim → live

SPEC Testing Strategy: "dry run on live LSE data before sim; sim before live". Checkpoint F
(`tasks/plan.md`) needs SC4 and SC5 in all three stages. Tick a line only after it was run, with
the date. The add-on's own checklist (SC6: bracket, flatten, cancel, dedupe, 16:40 flatten, token
rejection, Python stopped) is `addon/CHECKLIST.md` (#68); it comes before stage 2.

## Stage 1 — dry run on live data (#82)

- [x] Passed 2026-10-14: see `docs/data-gate.md`, "Dry run on live data (#82)". Arm and disarm
  through the API, 24 bars journalled, stale pause and resume, no NinjaTrader add-on.
- SC5 was not exercised: ORB had already broken out before the arm, so no Bracket was logged. A
  dry-run Bracket is journalled as an `action` row with `sent: false` and never posted.

## Stage 2 — NinjaTrader Sim account (#99)

Run on a weekday during regular hours, Fenrir native (as in #82) on the machine that runs it.

### Before the session

- [ ] Add-on installed and checked per `addon/INSTALL.md` §4 (401 without the token, `/accounts`
  lists the sim account with `is_sim: true`), with the sim account (e.g. `Sim101`) in
  `managed_accounts` and `flatten_at` at `16:40`.
- [ ] `addon/CHECKLIST.md` (#68) run on the same NinjaTrader install.
- [ ] `.env` (or Settings page) has `FENRIR_NT_URL` (the NinjaTrader machine, port from the add-on
  config) and `FENRIR_NT_TOKEN`, plus the LSE key. No gate recording running: LSE allows one
  socket per key (`docs/data-gate.md`, Client API).
- [ ] Bar store current (`fenrir data pull` through the last trading day, else warm-up alerts and
  takes no entries) and a backtest of the strategy on the account type and contract, which arming requires.
- [ ] `fenrir serve`; status shows `nt.connected: true` and `lse.connected: true`.

### Arm and trade

- [ ] Arm on the Strategies page: mode `sim`, the sim account, phase, and NinjaTrader's start and
  peak balance (DEC-4a). The account waits for the add-on's first `account` message with a
  balance above 0, reconciles, then warms up; entries start once no condition is shown.
- [ ] Collect at least 10 fills: `gate_item5.py` fails below 10. ORB enters at most once a day, so
  an entry and its exit give about 2 fills a session (more on partial fills): plan on about 5
  sessions, or use a strategy that trades more. Every day traded goes into the same run below.
- [ ] Each entry: in NinjaTrader's Orders tab, the stop and target (OCO, order names `F|<id>|S`
  and `F|<id>|T`) are working at the filled quantity while the position is open
  (`curl -s -H "X-Fenrir-Token: $T" "$H/orders?account=Sim101"` shows `role: "stop"`).
- [ ] No `drift` pause, or each one noted: an entry filled more than 4 ticks from the close it was
  checked at pauses entries until Resolve (`docs/contracts.md`, Orders and fills (22.1)).
- [ ] The runner's own Flatten goes out on the bar that closes at `min(flat_by, 16:40)`, and the
  add-on flattens and cancels at 16:40 ET by itself.

Python or Fenrir offline at 16:40: the add-on still flattens, but its exit fill never reaches the
journal. At the next start the replayed journal still holds the position while NinjaTrader is flat,
so the reconcile reports a mismatch and pauses entries: check NinjaTrader is flat, then Resolve on
the Live page. A restart only in a later trading day (after 18:00 ET) replays no fills, so it
reconciles, but an `end_of_day` account then halts with "confirm balances" for the close it missed
(SPEC DEC-4a): confirm the peak and the liquidation level.

### SC5 from the journal

Every Bracket sent has a stop on the losing side of its target, no position was left without a
known stop, and any Bracket rejected is journalled with its reason. `fills under them` counts the
fills carrying a sent Bracket's client id (its entry, stop and target: the add-on names them
`F|<id>|E/S/T`; a Flatten's fills carry none).
From the repo root, read-only (pass every day traded):

```bash
uv run python - Sim101 2026-10-15 2026-10-16 <<'EOF'
import sys
from datetime import date
from fenrir.live import journal
from fenrir.settings import load_settings

account, days = sys.argv[1], [date.fromisoformat(d) for d in sys.argv[2:]]
conn = journal.open_ro(load_settings().private_dir / "fenrir.db")
rows = [r for d in days for r in journal.rows(conn, account, d)]
sent = [r.payload for r in rows if r.kind == "action" and r.payload["type"] == "Bracket"
        and r.payload["sent"]]
bad = [a for a in sent if not (a["stop"] < a["target"] if a["side"] == "long"
                               else a["stop"] > a["target"])]
ids = {a["client_id"] for a in sent}
under = [r for r in rows if r.kind == "fill" and r.payload["client_id"] in ids]
stopless = [r.payload["message"] for r in rows if r.kind == "alert"
            and "has no known stop" in r.payload["message"]]
rejected = [r.payload for r in rows if r.kind == "rejection"
            and r.payload["action"]["type"] == "Bracket"]
print(f"brackets sent={len(sent)} stop on the wrong side={len(bad)} fills under them={len(under)}"
      f" no-known-stop alerts={len(stopless)} brackets rejected={len(rejected)}")
for x in [*bad, *stopless, *rejected]:
    print("  ", x)
EOF
```

- [ ] `stop on the wrong side=0` and `no-known-stop alerts=0`; each rejection's reason read.
- [ ] Every entry's stop was seen working at NinjaTrader (above): the journal holds what was sent,
  not what NinjaTrader placed.

### Gate item 5

- [ ] `uv run python scripts/gate_item5.py Sim101 --day <each day traded>` (add `--db PATH` when
  `FENRIR_PRIVATE_DIR` is not set in `.env`). It reads only the journal: each fill against the
  journalled LSE bar of its minute, PASS at 10 or more fills compared, none more than 1 tick
  outside its bar's [low, high]. Unmatched fills (no bar that minute, e.g. the server was down)
  are listed and not compared.
- [ ] The printed counts, worst distance and PASS/FAIL recorded in `docs/data-gate.md` Item 5,
  with the dates, account, strategy and contract.

## Stage 3 — live (after stage 2 passes)

- [ ] The live account in the add-on's `managed_accounts`; `is_sim: false` at `/accounts`.
- [ ] Arm with the live confirmation (SC4): backtest summary, `dll_on`, rules hash and liquidation
  level, the last checked against Lucid's dashboard.
- [ ] Smallest size the strategy allows; SC5 checked from the journal and at NinjaTrader as in
  stage 2.
- [ ] Kill switch and Resolve known before the first session (Live page).
