# Server Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A FastAPI server, started by `fenrir serve`, that listens on `127.0.0.1` only, requires a per-start session token on every `/api` request and WebSocket, checks WebSocket `Origin`, and serves the built UI with the token injected into `index.html`.

**Architecture:** `fenrir.server.auth` holds pure checks (`token_ok`, `origin_ok`, `allowed_origins`) plus two thin framework adapters (`require_token`, `check_ws`). `fenrir.server.app.create_app` wires routers, a `TrustedHostMiddleware` (blocks DNS-rebinding), and a catch-all that serves static files or `index.html` with the token injected by the pure `inject_token`. `fenrir.cli.serve` generates the token and calls `uvicorn.run` on `127.0.0.1`.

**Tech Stack:** Python 3.12, FastAPI, Starlette `TrustedHostMiddleware`, uvicorn, Typer, pytest, FastAPI `TestClient` (httpx).

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§7 security requirements, §12 security tests)

**Index:** `docs/superpowers/plans/README.md` (contracts: "Plan 11 — server", "Plan 01 — settings and CLI")

## Global Constraints

- The server listens on `127.0.0.1` only (Docker publishes `127.0.0.1:8000:8000`, never `8000:8000`).
- A random session token is generated at startup: `secrets.token_urlsafe(32)`.
- The UI receives the token from the served page as `<meta name="fenrir-token" content="...">`.
- Every REST request carries header `X-Fenrir-Token`; every WebSocket carries `?token=`. Bad token on REST → 401. Bad token or foreign `Origin` on WebSocket → closed with code 4401.
- Allowed WebSocket origins: `http://127.0.0.1:<port>` and `http://localhost:<port>` (plus the Vite dev server `http://127.0.0.1:5173` / `http://localhost:5173` only under `--dev`).
- `GET /api/dev-token` exists only under `fenrir serve --dev`.
- Routers live in `src/fenrir/server/routes/<area>.py` as `router = APIRouter(prefix="/api/<area>")`, registered in `create_app` with one line; every `/api` router gets `dependencies=[Depends(require_token)]`.
- Functional core, imperative shell: handlers only forward.

## Review Focus

1. **DNS rebinding** — a page on `http://evil.example:8000` whose DNS points at 127.0.0.1 must not be able to read `index.html` (and so the token). Expect: requests whose `Host` is not `127.0.0.1`/`localhost` get 400. Test: `test_foreign_host_header_rejected` (Task 3).
2. **Path traversal through the static catch-all** — `GET /..%2f..%2fpyproject.toml` must not leak files outside `static_dir`. Expect: falls back to `index.html`. Test: `test_static_does_not_escape_dir` (Task 3).
3. **Unknown `/api/...` path** — must be a 404, not the SPA's `index.html` with a 200 (which would hide client bugs and leak the token to any API probe). Test: `test_unknown_api_path_is_404` (Task 3).
4. **Missing `Origin` header on a WebSocket** (non-browser client with a stolen token) — must be rejected like a foreign origin. Test: `test_ws_without_origin_rejected` (Task 2).
5. **Token timing** — token comparison must be constant-time (`secrets.compare_digest`) and an empty expected token must never match an empty header. Test: `test_token_ok_rejects_empty` (Task 1).

---

### Task 1: Pure auth checks and token injection

**Files:**
- Create: `src/fenrir/server/__init__.py`
- Create: `src/fenrir/server/auth.py`
- Create: `src/fenrir/server/static.py`
- Create: `src/fenrir/server/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/server/test_auth.py`, `tests/server/test_static.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `token_ok(expected: str, given: str | None) -> bool`
  - `allowed_origins(port: int, dev: bool) -> frozenset[str]`
  - `origin_ok(origin: str | None, allowed: frozenset[str]) -> bool`
  - `inject_token(html: str, token: str) -> str`

- [ ] **Step 1: Add dependencies**

Run: `uv add fastapi "uvicorn[standard]" httpx`
Expected: `pyproject.toml` lists `fastapi`, `uvicorn[standard]`, `httpx`; `uv.lock` updated.

- [ ] **Step 2: Write the failing tests**

`tests/server/test_auth.py`:

```python
from fenrir.server.auth import allowed_origins, origin_ok, token_ok


def test_token_ok_matches():
    assert token_ok("abc", "abc")


def test_token_ok_rejects_wrong_and_missing():
    assert not token_ok("abc", "abd")
    assert not token_ok("abc", None)


def test_token_ok_rejects_empty():
    assert not token_ok("", "")
    assert not token_ok("", None)


def test_allowed_origins_prod():
    assert allowed_origins(8000, dev=False) == frozenset(
        {"http://127.0.0.1:8000", "http://localhost:8000"}
    )


def test_allowed_origins_dev_adds_vite():
    assert "http://localhost:5173" in allowed_origins(8000, dev=True)
    assert "http://127.0.0.1:5173" in allowed_origins(8000, dev=True)


def test_origin_ok():
    allowed = allowed_origins(8000, dev=False)
    assert origin_ok("http://localhost:8000", allowed)
    assert not origin_ok("http://evil.example:8000", allowed)
    assert not origin_ok("http://localhost:8001", allowed)
    assert not origin_ok(None, allowed)
```

`tests/server/test_static.py`:

```python
from fenrir.server.static import inject_token


def test_inject_token_into_head():
    html = "<html><head><title>x</title></head><body></body></html>"
    out = inject_token(html, "tok123")
    assert '<meta name="fenrir-token" content="tok123"></head>' in out
    assert out.count("fenrir-token") == 1


def test_inject_token_only_first_head():
    out = inject_token("<head></head><head></head>", "t")
    assert out.count("fenrir-token") == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/server -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.server'`

- [ ] **Step 4: Write minimal implementation**

`src/fenrir/server/__init__.py`: empty file.

`src/fenrir/server/auth.py`:

```python
"""Session-token and Origin checks. Pure checks first; framework adapters last."""

import secrets

from fastapi import Header, HTTPException, Request, WebSocket

VITE_PORT = 5173


def token_ok(expected: str, given: str | None) -> bool:
    return bool(expected) and given is not None and secrets.compare_digest(expected, given)


def allowed_origins(port: int, dev: bool) -> frozenset[str]:
    ports = (port, VITE_PORT) if dev else (port,)
    return frozenset(f"http://{host}:{p}" for host in ("127.0.0.1", "localhost") for p in ports)


def origin_ok(origin: str | None, allowed: frozenset[str]) -> bool:
    return origin in allowed


def require_token(request: Request, x_fenrir_token: str | None = Header(default=None)) -> None:
    if not token_ok(request.app.state.token, x_fenrir_token):
        raise HTTPException(status_code=401, detail="missing or bad session token")


def check_ws(ws: WebSocket) -> bool:
    state = ws.app.state
    return token_ok(state.token, ws.query_params.get("token")) and origin_ok(
        ws.headers.get("origin"), state.origins
    )
```

`src/fenrir/server/static.py`:

```python
def inject_token(html: str, token: str) -> str:
    # token_urlsafe output needs no HTML escaping
    return html.replace("</head>", f'<meta name="fenrir-token" content="{token}"></head>', 1)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/server -q`
Expected: `8 passed`

- [ ] **Step 6: Add the directory context file and map row**

`src/fenrir/server/CLAUDE.md`:

```markdown
# src/fenrir/server/

The FastAPI server: session-token auth, Origin checks, static UI serving, and one router module per UI area.

- Entry points: `app.py` (`create_app`), `auth.py` (`require_token`, `check_ws`), `routes/<area>.py` (`router`, registered in `create_app`).
- Pure vs shell: `auth.py` checks and `static.py` are pure; handlers in `routes/` only parse the request and forward to the core package that owns the logic.
- Security: binds 127.0.0.1 only; every `/api` router uses `require_token`; every WebSocket calls `check_ws` before `accept()`. `GET /api/health` is the only unauthenticated route.
- Not here: business logic (backtests in `fenrir.backtest`, rules in `fenrir.rules`, live trading in `fenrir.live`). The CLI entry `fenrir serve` is in `fenrir/cli/serve.py`.
- Tests: `tests/server/`.
```

Add this row to the root `CLAUDE.md` Directory Map table, after the `docs/superpowers/plans/` row:

```markdown
| `src/fenrir/server/` | FastAPI app: session token, Origin and Host checks, static UI, one router per UI area in `routes/`. |
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/fenrir/server tests/server CLAUDE.md
git commit -m "feat(server): add session token, origin checks and token injection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `create_app` with health, dev token and WebSocket check

**Files:**
- Create: `src/fenrir/server/app.py`
- Test: `tests/server/test_app.py`

**Interfaces:**
- Consumes: `Settings` (plan 01, `fenrir.settings`), `allowed_origins`, `require_token`, `check_ws`, `inject_token` (Task 1).
- Produces: `create_app(settings: Settings, token: str, static_dir: Path | None, *, port: int = 8000, dev: bool = False) -> FastAPI`. `app.state.token`, `app.state.origins`, `app.state.settings` are set for later routers. `GET /api/health` → `{"ok": true}` (no token). `GET /api/dev-token` → `{"token": ...}` only when `dev=True`.

- [ ] **Step 1: Write the failing tests**

`tests/server/test_app.py`:

```python
import pytest
from fastapi import Depends, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fenrir.server.app import create_app
from fenrir.server.auth import check_ws, require_token
from fenrir.settings import Settings

TOKEN = "test-token"
BASE = "http://127.0.0.1:8000"
GOOD_ORIGIN = {"origin": "http://127.0.0.1:8000"}


def make_client(dev: bool = False) -> TestClient:
    app = create_app(Settings(), TOKEN, None, port=8000, dev=dev)

    @app.get("/api/probe", dependencies=[Depends(require_token)])
    def probe() -> dict:
        return {"ok": True}

    @app.websocket("/ws/probe")
    async def ws_probe(ws: WebSocket) -> None:
        if not check_ws(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        await ws.send_text("hello")
        await ws.close()

    return TestClient(app, base_url=BASE)


def test_health_needs_no_token():
    assert make_client().get("/api/health").json() == {"ok": True}


def test_api_rejects_missing_token():
    assert make_client().get("/api/probe").status_code == 401


def test_api_rejects_wrong_token():
    r = make_client().get("/api/probe", headers={"X-Fenrir-Token": "nope"})
    assert r.status_code == 401


def test_api_accepts_token():
    r = make_client().get("/api/probe", headers={"X-Fenrir-Token": TOKEN})
    assert r.json() == {"ok": True}


def test_ws_accepts_token_and_origin():
    with make_client().websocket_connect(f"/ws/probe?token={TOKEN}", headers=GOOD_ORIGIN) as ws:
        assert ws.receive_text() == "hello"


def test_ws_rejects_foreign_origin():
    with pytest.raises(WebSocketDisconnect) as e:
        with make_client().websocket_connect(
            f"/ws/probe?token={TOKEN}", headers={"origin": "http://evil.example"}
        ):
            pass
    assert e.value.code == 4401


def test_ws_without_origin_rejected():
    with pytest.raises(WebSocketDisconnect) as e:
        with make_client().websocket_connect(f"/ws/probe?token={TOKEN}"):
            pass
    assert e.value.code == 4401


def test_ws_rejects_bad_token():
    with pytest.raises(WebSocketDisconnect) as e:
        with make_client().websocket_connect("/ws/probe?token=nope", headers=GOOD_ORIGIN):
            pass
    assert e.value.code == 4401


def test_dev_token_only_in_dev():
    assert make_client(dev=False).get("/api/dev-token").status_code == 404
    assert make_client(dev=True).get("/api/dev-token").json() == {"token": TOKEN}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.server.app'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/server/app.py`:

```python
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fenrir.server.auth import allowed_origins
from fenrir.settings import Settings


def create_app(
    settings: Settings,
    token: str,
    static_dir: Path | None,
    *,
    port: int = 8000,
    dev: bool = False,
) -> FastAPI:
    app = FastAPI(title="Fenrir Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.token = token
    app.state.origins = allowed_origins(port, dev)
    # Rejects DNS-rebinding requests whose Host is not loopback.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    if dev:

        @app.get("/api/dev-token")
        def dev_token() -> dict:
            return {"token": token}

    # Register routers here, one line each:
    # app.include_router(<area>.router, dependencies=[Depends(require_token)])

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/server -q`
Expected: `17 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/server/app.py tests/server/test_app.py
git commit -m "feat(server): add create_app with health, dev token and trusted hosts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Serve the built UI with the token injected

**Files:**
- Modify: `src/fenrir/server/app.py`
- Test: `tests/server/test_spa.py`

**Interfaces:**
- Consumes: `inject_token` (Task 1), `create_app` (Task 2).
- Produces: when `static_dir` is given, `GET /<path>` returns the file if it exists inside `static_dir`, else `index.html` with the token meta tag. `/api/*` and `/ws/*` paths never fall back. The catch-all is registered last; later plans add routers above it.

- [ ] **Step 1: Write the failing tests**

`tests/server/test_spa.py`:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fenrir.server.app import create_app
from fenrir.settings import Settings

BASE = "http://127.0.0.1:8000"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html><head></head><body>app</body></html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    return TestClient(create_app(Settings(), "tok", static), base_url=BASE)


def test_index_has_token(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert '<meta name="fenrir-token" content="tok">' in r.text


def test_spa_route_falls_back_to_index(client: TestClient):
    assert "fenrir-token" in client.get("/backtests/abc").text


def test_static_file_served(client: TestClient):
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_static_does_not_escape_dir(client: TestClient):
    r = client.get("/..%2fsecret.txt")
    assert "do not serve" not in r.text


def test_unknown_api_path_is_404(client: TestClient):
    assert client.get("/api/nope").status_code == 404


def test_foreign_host_header_rejected(client: TestClient):
    r = client.get("/", headers={"host": "evil.example:8000"})
    assert r.status_code == 400
    assert "fenrir-token" not in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_spa.py -q`
Expected: FAIL — `test_index_has_token` gets 404.

- [ ] **Step 3: Write minimal implementation**

In `src/fenrir/server/app.py`, add imports:

```python
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from fenrir.server.static import inject_token
```

and replace the final `return app` with:

```python
    if static_dir is not None:
        _mount_spa(app, static_dir.resolve(), token)

    return app


def _mount_spa(app: FastAPI, root: Path, token: str) -> None:
    index = HTMLResponse(inject_token((root / "index.html").read_text(), token))

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "ws/")):
            raise HTTPException(status_code=404)
        file = (root / path).resolve()
        if path and file.is_file() and file.is_relative_to(root):
            return FileResponse(file)
        return index
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/server -q`
Expected: `23 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/server/app.py tests/server/test_spa.py
git commit -m "feat(server): serve built ui with session token injected

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `fenrir serve` bound to 127.0.0.1

**Files:**
- Create: `src/fenrir/cli/serve.py`
- Modify: `src/fenrir/cli/main.py`
- Test: `tests/cli/test_serve.py`

**Interfaces:**
- Consumes: `create_app` (Task 2), `load_settings` (plan 01), `app` in `fenrir.cli.main` (plan 01).
- Produces: `fenrir serve [--port 8000] [--dev] [--static-dir web/dist]`. `serve` is a single command, registered with `app.command("serve")(serve.serve)` (a group would need a subcommand).

- [ ] **Step 1: Write the failing test**

`tests/cli/test_serve.py`:

```python
from typer.testing import CliRunner

from fenrir.cli import serve as serve_mod
from fenrir.cli.main import app


def test_serve_binds_loopback_only(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda a, **kw: calls.update(kw))
    result = CliRunner().invoke(app, ["serve", "--port", "8123", "--static-dir", str(tmp_path / "none")])
    assert result.exit_code == 0, result.output
    assert calls == {"host": "127.0.0.1", "port": 8123}


def test_serve_without_build_prints_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda a, **kw: None)
    result = CliRunner().invoke(app, ["serve", "--static-dir", str(tmp_path / "none")])
    assert "UI not built" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_serve.py -q`
Expected: FAIL with `ImportError: cannot import name 'serve' from 'fenrir.cli'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/cli/serve.py`:

```python
import secrets
from pathlib import Path

import typer
import uvicorn

from fenrir.server.app import create_app
from fenrir.settings import load_settings


def serve(
    port: int = typer.Option(8000, help="Port on 127.0.0.1"),
    dev: bool = typer.Option(False, help="Allow the Vite dev server origin and expose /api/dev-token"),
    static_dir: Path = typer.Option(Path("web/dist"), help="Built UI directory"),
) -> None:
    """Start the Fenrir Desk server on 127.0.0.1."""
    static = static_dir if (static_dir / "index.html").is_file() else None
    if static is None:
        typer.echo(f"UI not built ({static_dir}/index.html missing); serving the API only.")
    app = create_app(load_settings(), secrets.token_urlsafe(32), static, port=port, dev=dev)
    typer.echo(f"Fenrir Desk on http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)
```

In `src/fenrir/cli/main.py`, add the import next to the other `fenrir.cli` imports and the registration after the other `app.add_typer` lines:

```python
from fenrir.cli import serve
```

```python
app.command("serve")(serve.serve)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all tests pass, including `tests/cli/test_serve.py` (2 passed).

- [ ] **Step 5: Manual check**

Run: `uv run fenrir serve --port 8000` and in another terminal:

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/dev-token
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Expected: `{"ok":true}`, then `404`, then one `LISTEN` line on `127.0.0.1:8000` (not `*:8000`). Stop the server with Ctrl-C.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: no errors.

```bash
git add src/fenrir/cli/serve.py src/fenrir/cli/main.py tests/cli/test_serve.py
git commit -m "feat(server): add fenrir serve bound to 127.0.0.1

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
