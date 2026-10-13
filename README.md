# Fenrir Desk

A local-only, browser-based workbench for prop traders. Fenrir Desk helps you design, backtest, and run trading strategies built around the rules of specific prop firm challenges, with the goal of passing evaluations and reaching payouts as quickly and safely as possible.

> **Status:** early development. The core, rules, strategy contract, history pull and single-attempt backtest are built; the UI and live trading are not. Nothing here is usable yet.

The full v1 spec is [SPEC.md](SPEC.md).

## Goals

- **Prop firm rules library.** A structured, versioned library of challenge and funded-account rules (profit targets, daily loss limits, trailing drawdown, consistency rules, minimum trading days, contract limits, payout requirements). v1 covers Lucid Trading only.
- **Rule-aware strategy generation.** Claude Code writes and tunes deterministic strategies that treat a firm's rules as hard constraints, optimizing for time-to-pass and time-to-payout instead of raw profit.
- **Backtesting on deep history.** Pull historical market data from an external provider so strategies can be tested across many years and market regimes, and simulate the firm's rules bar by bar.
- **NinjaTrader execution.** Connect to NinjaTrader to place and manage trades for live or sim accounts.
- **Local only.** Runs on your machine and is served to your browser from localhost. No hosted backend and no accounts. Credentials and data are stored only on your machine; the LSE API key is sent only to LSE, to authenticate market data requests.

## Stack

- **Backend and CLI:** Python 3.12, `uv`, FastAPI, Typer, Polars + Parquet.
- **UI:** Vite, React, TypeScript, TanStack Router + Query, Tailwind, shadcn/ui.
- **Market data:** London Strategic Edge (LSE) for history. LSE is also the intended live source, pending data gate item 3 (real-time futures access).
- **Execution:** a C# NinjaScript add-on in NinjaTrader 8 (Windows), over local HTTP + WebSocket.

## Run

Requires Docker (Docker Desktop on macOS or Windows).

```
git clone https://github.com/rrhodeswebdev/fenrir-desk.git && cd fenrir-desk
docker compose up
```

Open the launch link it prints (`docker compose logs` shows it again), then enter the LSE key and the NinjaTrader address and token on the Settings page. They are kept in the `fenrir-private` Docker volume, never in the repo. The first time you run `claude` in the Workbench terminal, sign in; the login is kept in the `claude-config` volume and survives rebuilds.

- **This machine only.** The app includes a shell. Keep the `127.0.0.1:8000:8000` and `[::1]:8000:8000` port mappings in `compose.yaml`; never change them to `8000:8000`.
- **Terminal.** It runs as an unprivileged `claude` user in `/app` (your clone), and cannot read the secrets, the session key or the server. Run CLI commands there, e.g. `uv run fenrir backtest strategies/orb.py --account lucid-flex-50k`. Each backtest worker holds a full price series (up to about 2 GB), and workers are capped to fit the container's memory, so a larger Docker Desktop memory limit runs more of them.
- **In Docker, Claude edits strategies, rules and data only.** The repo is mounted read-only; only `data/`, `rules/` and `strategies/` are writable, so nothing in the terminal can change `.git`, `.claude/`, the Docker files, `.env`, the app's code or anything else your git, Claude Code or `docker compose` read. Edit the app's code natively (see Develop). Claude can read history (`git log`, `git diff`) but cannot commit; you review and commit on the host. Tools that write caches in the repo root (pytest, ruff) get a read-only error there.
- **No secrets in the repo `.env`.** The app never reads it in Docker, but the terminal can read every file in the repo, `.env` included. Keep the keys on the Settings page.
- **Before running git after a terminal session.** The terminal can create a `.git` inside `data/`, `rules/` or `strategies/`, and your git, a git-aware shell prompt or an editor in that folder would run its config on your machine. From the repo root, first run `find data rules strategies -name .git`: it must print nothing (the Settings page and `docker compose logs` warn when it would not). Then review with `git status --short --ignored` and `git diff`, and commit yourself. Review a strategy before running it natively: it is Python.
- **Rebuilds.** The server runs code built into the image, so changes you make to `src/`, `web/`, `assets/brand/`, `pyproject.toml` or `uv.lock` take effect after `docker compose up --build`.
- **Linux hosts.** Build with your uid so files the terminal writes are yours, every time, then start: `docker compose build --build-arg CLAUDE_UID=$(id -u) && docker compose up` (a plain `up --build` would rebuild with uid 1000).
- **`docker compose exec`.** Add `-u claude` for anything you run inside; without it the command runs as root.
- **NinjaTrader address.** `http://host.docker.internal:8123` when NinjaTrader runs on the same Windows machine; `http://<VM IP>:8123` for NinjaTrader in a Windows VM on a Mac (run `ipconfig` in the VM; use bridged or shared networking). Live trading needs NinjaTrader 8 with the Fenrir add-on imported: see [addon/INSTALL.md](addon/INSTALL.md).

## Windows Firewall

The add-on listens on port 8123 on the NinjaTrader machine. Allow it from your local network only, in PowerShell as Administrator:

```powershell
netsh http add urlacl url=http://+:8123/ user=Everyone
netsh advfirewall firewall add rule name="Fenrir add-on 8123" dir=in action=allow protocol=TCP localport=8123 remoteip=localsubnet
```

With Docker on the same Windows machine, if requests time out the Docker/WSL network is not in `localsubnet`: run `ipconfig`, find the `vEthernet (WSL)` adapter's subnet and use it as `remoteip=` instead (for example `remoteip=172.16.0.0/12`).

## Develop

Natively, the Workbench terminal runs as you and is not isolated; secrets come from `.env` (`cp .env.example .env`).

```
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run fenrir data pull           # needs FENRIR_LSE_API_KEY in .env
uv run fenrir data summary
uv run fenrir serve --dev         # API on localhost:8000
npm --prefix web ci
npm --prefix web run dev          # UI with hot reload; open the :5173 launch link serve prints
```

## Disclaimer

Fenrir Desk is a personal software project, not financial advice. Trading futures and other leveraged products carries substantial risk of loss. Prop firm rules change often; always verify the current rules with the firm before relying on anything in this library. Use at your own risk.

## License

[MIT](LICENSE)
