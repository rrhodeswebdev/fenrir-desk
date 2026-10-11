# Lucid rule mechanics to confirm

The rules YAML holds Lucid's numbers; these are the **semantics** the code in `src/fenrir/rules/` assumes. Each one is a single function, so a wrong assumption is a one-file fix. The user should confirm them against Lucid's rules pages before plan 09 reports are trusted.

| Rule | Assumption | Where |
|---|---|---|
| EOD trailing drawdown | Floor moves only at day close, from end-of-day equity; any intrabar equity at or below the floor breaches. | `drawdown.py` |
| Drawdown lock | `floor = min(peak, locks_at) - amount`; the floor never moves down. | `drawdown.py` |
| Intraday trailing | High-water follows the bar's best unrealized equity; within a bar the high is assumed to come before the low. | `drawdown.py` |
| Daily loss limit | A day halt, not a failure (Lucid: "The DLL is a soft breach, so if you hit it you will simply be locked out until the next trading session", support.lucidtrading.com, LucidFlex Customization). Hitting it sets `halted_today`; engines flatten the open position, `pretrade.check` refuses entries, and `ledger.book` clears it at `DayClosed`. The rules amount applies only when the user turned the DLL on for that account (`dll_on`, `<data_dir>/accounts.yaml`); `with_dll` turns it off otherwise, so every other function only ever sees effective rules. | `daily_loss.py`, `ledger.py`, `pretrade.py`, `account.py` |
| Consistency | Largest single day ≤ `max_day_pct`% of total net profit over all traded days (today included). It blocks passing or payout; it never fails the attempt. | `consistency.py` |
| Minimum days | Counts days with at least one fill. | `account.py` |
| Pass | Realized `balance >= profit_target`, flat, and the gates above. | `account.py` |
| First payout | `balance × max_pct_of_profit% × split_pct%`, once `min_days`, `min_profit` and consistency hold, while flat. No buffer rule. | `account.py` |
| Microscalp | 1-minute bars cannot show 5-second holds, so same-bar winners are only **flagged** (`possible_microscalp_pnl`); they never breach or block payout. | `microscalp.py` |
| Contract limit | Enforced as a pre-trade rejection, never as an account breach. | `contracts.py`, `pretrade.py` |
| Flat-by | Holding a position on any bar starting at or after `flat_by` breaches; so does a day close while not flat. | `flat_by.py` |

