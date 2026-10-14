# Fenrir Desk

A local-only, browser-based workbench for prop traders. Fenrir Desk helps you design, backtest, and run trading strategies built around the rules of specific prop firm challenges, with the goal of passing evaluations and reaching payouts as quickly and safely as possible.

> **Status:** pre-release. Built: the rules library, strategy contract, history pull, backtests, the browser UI (Workbench, Rules, Data, Backtests, Strategies, Live, Settings), the live runner on LSE bars and the NinjaTrader add-on. Dry runs on live data work. Live trading is not cleared yet: it waits on the add-on's Sim checklist ([#68](https://github.com/rrhodeswebdev/fenrir-desk/issues/68)) and gate item 5, sim fills checked against live bars ([#99](https://github.com/rrhodeswebdev/fenrir-desk/issues/99)). Use dry or sim accounts only until then.

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
- **Market data:** London Strategic Edge (LSE), for history and live bars. The data gate's live items passed: real-time futures on 2026-10-13, and live-built bars matching history on 2026-10-14 ([docs/data-gate.md](docs/data-gate.md)). A key allows one live connection: while an account is armed the app holds it, and it frees it within a few seconds once nothing is armed.
- **Execution:** a C# NinjaScript add-on in NinjaTrader 8 (Windows), over local HTTP + WebSocket.

## Run

Requires Docker (Docker Desktop on macOS or Windows). On Linux, once: `sudo usermod -aG docker $USER` (then log out and back in) and `sudo systemctl enable --now docker`, so Docker runs without sudo and starts at boot.

```
git clone https://github.com/rrhodeswebdev/fenrir-desk.git && cd fenrir-desk
docker compose up
```

That is the only command. Open the launch link it prints (`docker compose logs` shows it again); the browser then stays signed in for 400 days, across restarts and rebuilds (the session key is kept in the `fenrir-private` volume), so the link is needed once per browser. Ctrl+C stops the app; `docker compose up -d` runs it in the background instead, and Docker starts it again after a reboot (`restart: unless-stopped`). Then enter the LSE key and the NinjaTrader address and token on the Settings page. They are kept in the `fenrir-private` Docker volume, never in the repo. The first time you run `claude` in the Workbench terminal, sign in; the login is kept in the `claude-config` volume and survives rebuilds.

- **This machine only.** The app includes a shell. Keep the `127.0.0.1:8000:8000` and `[::1]:8000:8000` port mappings in `compose.yaml`; never change them to `8000:8000`.
- **Terminal.** It runs as an unprivileged `claude` user in `/app` (your clone), and cannot read the secrets, the session key or the server. Run CLI commands there, e.g. `uv run fenrir backtest strategies/orb.py --account lucid-flex-50k`. Each backtest worker holds a full price series (up to about 2 GB), and workers are capped to fit the container's memory, so a larger Docker Desktop memory limit runs more of them.
- **In Docker, Claude edits strategies, rules and data only.** The repo is mounted read-only; only `data/`, `rules/` and `strategies/` are writable, so nothing in the terminal can change `.git`, `.claude/`, the Docker files, `.env`, the app's code or anything else your git, Claude Code or `docker compose` read. Edit the app's code natively (see Develop). Claude can read history (`git log`, `git diff`) but cannot commit; you review and commit on the host. Tools that write caches in the repo root (pytest, ruff) get a read-only error there. Claude Code starts in `/app` and reads the project's `.claude/` skill and `CLAUDE.md` files; its own settings and login live in the `claude-config` volume, and anything it would save to the project (`.claude/settings.local.json`, an edited `CLAUDE.md`) fails, so choose "allow once" rather than "always allow for this project".
- **No secrets in the repo `.env`.** The app never reads it in Docker, but the terminal can read every file in the repo, `.env` included. Keep the keys on the Settings page.
- **Before running git after a terminal session.** The terminal can create a `.git` (in any case: macOS git also uses `.GIT`) inside `data/`, `rules/` or `strategies/`, and your git, a git-aware shell prompt or an editor in that folder would run its config on your machine. From the repo root, first run `find data rules strategies -iname .git`: it must print nothing (the Settings page and `docker compose logs` warn when it would not). Then review with `git status --short --ignored` and `git diff`, and commit yourself. Review a strategy before running it natively: it is Python.
- **Rebuilds.** The server runs code built into the image, so changes you make to `src/`, `web/`, `assets/brand/`, `pyproject.toml` or `uv.lock` take effect after `docker compose up --build`.
- **Linux hosts.** The image's terminal user has uid 1000, the usual first user's (`id -u`), so files it writes are yours. With another uid, build with it, every time, then start: `docker compose build --build-arg CLAUDE_UID=$(id -u) && docker compose up` (a plain `up --build` would rebuild with uid 1000).
- **`docker compose exec`.** Add `-u claude` for anything you run inside; without it the command runs as root.
- **NinjaTrader.** Sim and live trading need NinjaTrader 8 with the Fenrir add-on: see [NinjaTrader add-on](#ninjatrader-add-on).

## NinjaTrader add-on

Fenrir trades through a C# add-on inside NinjaTrader 8 on Windows: the same machine as Docker, or a Windows VM when Fenrir runs on a Mac or Linux host. Full steps and config: [addon/INSTALL.md](addon/INSTALL.md). Checks on Sim101: [addon/CHECKLIST.md](addon/CHECKLIST.md).

1. **Import.** `python3 addon/build.py` writes `addon/dist/FenrirAddOn.zip`. In NinjaTrader: **Tools → Import → NinjaScript Add-On…**, pick the zip; NinjaTrader compiles it. The zip layout is not yet confirmed on a clean NinjaTrader ([CHECKLIST](addon/CHECKLIST.md) A2, A3); if the import fails, install from source ([addon/INSTALL.md](addon/INSTALL.md), section 1).
2. **Token.** Generate one with `openssl rand -hex 32`. Copy `addon/fenrir-addon.example.json` to `Documents\NinjaTrader 8\fenrir-addon.json`, put the token there and list the accounts Fenrir may trade in `managed_accounts`. Enter the same token on Fenrir's Settings page.
3. **Windows Firewall.** The add-on listens on port 8123. In PowerShell as Administrator on the NinjaTrader machine, allow it from your local network only, then restart NinjaTrader (**New → NinjaScript Output** shows `Fenrir: listening on port 8123`):

   ```powershell
   netsh http add urlacl url=http://+:8123/ user=Everyone
   netsh advfirewall firewall add rule name="Fenrir add-on 8123" dir=in action=allow protocol=TCP localport=8123 remoteip=localsubnet
   ```

   With Docker on the same Windows machine, if requests time out the Docker/WSL network is not in `localsubnet`: run `ipconfig`, find the `vEthernet (WSL)` adapter's subnet and use it as `remoteip=` instead (for example `remoteip=172.16.0.0/12`).
4. **Address.** On the Settings page: `http://host.docker.internal:8123` when NinjaTrader runs on the same Windows machine as Docker; `http://<VM IP>:8123` for NinjaTrader in a Windows VM (run `ipconfig` in the VM). The VM must be reachable from the host: on a Mac use bridged or shared networking; on a Linux host with VirtualBox, a NAT-only VM is not reachable without port forwarding, so add a host-only adapter (VM shut down: its **Settings → Network → Adapter 2**, **Attached to: Host-only Adapter**; [addon/INSTALL.md](addon/INSTALL.md), section 3) and use the VM's `192.168.56.x` address. Settings apply after `docker compose restart`; the Live page's NinjaTrader field then shows the heartbeat.

At 16:40 New York time the add-on flattens and cancels everything on its managed accounts by itself, whether Fenrir is running or not.

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
