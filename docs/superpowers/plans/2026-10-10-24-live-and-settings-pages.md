# Live and Settings Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the user a Live page (connections, per-account balance and distance to every limit, position, halt reason with Resolve, today's journal, kill switch) and a Settings page that edits `.env` without exposing secrets.

**Architecture:** The Live page is UI only over the plan 21/22 endpoints, polling `/api/live/status` with TanStack Query `refetchInterval`; the kill switch is a separate component that never waits on status, so it works when status fails. Settings adds two pure functions to `fenrir.settings` (`mask`, `update_env_text`) and a thin `/api/settings` router that validates the update with pydantic, rewrites `.env` in place, and returns the masked view. Saved settings apply on restart.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, pytest; React 19 + TypeScript, TanStack Query, Playwright. UI: Tailwind CSS v4 utility classes + shadcn/ui components (`@/components/ui/*`, set up in plan 12; add missing ones with `npx shadcn@latest add`); no custom CSS classes or inline styles except runtime-computed values.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§8 failure handling and kill switch, §10 Live and Settings pages)

**Index:** `docs/superpowers/plans/README.md` (contracts: "Plan 01 — settings", "Plan 11", "Plan 12", "Plans 21–24 — live API and storage")

## Global Constraints

- Functional core, imperative shell: `.env` editing is the pure `update_env_text(text, updates) -> str`; the handler only reads and writes the file.
- Settings keys: `FENRIR_` + upper-cased `Settings` field name (`nt_url` → `FENRIR_NT_URL`). Editable fields: `lse_api_key`, `nt_url`, `nt_token`, `holdout_months`, `safety_margin_pct`, `stale_seconds`, `heartbeat_policy`.
- Secrets (`lse_api_key`, `nt_token`) are never returned in full: `"••••" + last 4` when 12+ characters, else `"••••"`; empty stays `""`.
- `.env` is rewritten in place (Docker bind-mounts it as a single file, so write-to-temp-and-rename fails). Comments, blank lines and unknown keys are preserved.
- Kill switch: `POST /api/live/kill` after a confirmation; always clickable, even if status fails to load.
- Query keys: `['live', 'status']`, `['live', 'journal', account, day]`, `['settings']`.
- Commits: Conventional Commits, scopes `core`, `server`, `web`.

## Review Focus

1. `.env` with comments, blank lines, `export KEY=…` and keys Fenrir does not know: all kept after a save. (Task 1: `test_preserves_comments_blank_lines_and_unknown_keys`, `test_keeps_export_prefix`.)
2. A value with a space, quote, `#` or newline, or an out-of-range number: 422 and `.env` byte-for-byte unchanged. (Task 2: `test_invalid_values_are_rejected_and_file_untouched`.)
3. No `.env` yet (fresh clone that skipped `cp .env.example .env`): save creates it. (Task 2: `test_put_creates_missing_env_file`.)
4. Secrets never appear in full in any GET or PUT response, including right after saving them. (Task 2: `test_get_masks_secrets`, `test_put_writes_env_and_returns_masked_view`.)
5. Status endpoint failing (server mid-restart, runner crashed): kill switch still sends. Dismissing its confirmation sends nothing. (Task 4: `kill switch works while status is unavailable`, `dismissing the confirmation sends nothing`.)

---

### Task 1: Pure `.env` update and secret masking

**Files:**
- Modify: `src/fenrir/settings.py` (append two functions)
- Test: `tests/test_settings_env.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `mask(secret: str) -> str`; `update_env_text(text: str, updates: Mapping[str, str]) -> str` (sets each `KEY=value` where the key is defined, appends missing keys at the end, leaves every other line alone; raises `ValueError` if a value contains a newline).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings_env.py
import pytest

from fenrir.settings import mask, update_env_text


def test_mask_shows_last_four_of_long_secrets_only():
    assert mask("abcdefghijkl1234") == "••••1234"
    assert mask("short") == "••••"
    assert mask("") == ""


def test_replaces_existing_key_in_place():
    text = "FENRIR_NT_URL=http://old:8123\nFENRIR_STALE_SECONDS=90\n"
    assert update_env_text(text, {"FENRIR_NT_URL": "http://10.0.0.5:8123"}) == (
        "FENRIR_NT_URL=http://10.0.0.5:8123\nFENRIR_STALE_SECONDS=90\n")


def test_preserves_comments_blank_lines_and_unknown_keys():
    text = "# NinjaTrader\nFENRIR_NT_TOKEN=old\n\nOTHER_TOOL=1\n# FENRIR_STALE_SECONDS=30\n"
    out = update_env_text(text, {"FENRIR_NT_TOKEN": "new", "FENRIR_STALE_SECONDS": "60"})
    assert out == ("# NinjaTrader\nFENRIR_NT_TOKEN=new\n\nOTHER_TOOL=1\n"
                   "# FENRIR_STALE_SECONDS=30\nFENRIR_STALE_SECONDS=60\n")


def test_keeps_export_prefix():
    assert update_env_text("export FENRIR_NT_TOKEN=old\n", {"FENRIR_NT_TOKEN": "new"}) == "export FENRIR_NT_TOKEN=new\n"


def test_empty_file_gets_appended_keys():
    assert update_env_text("", {"FENRIR_HOLDOUT_MONTHS": "6"}) == "FENRIR_HOLDOUT_MONTHS=6\n"


def test_rejects_newline_in_value():
    with pytest.raises(ValueError):
        update_env_text("", {"FENRIR_NT_TOKEN": "a\nFENRIR_NT_URL=http://evil"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings_env.py -q`
Expected: FAIL with `ImportError: cannot import name 'mask' from 'fenrir.settings'`

- [ ] **Step 3: Append the implementation to `src/fenrir/settings.py`**

`Mapping` is already imported there for `parse_settings`; if not, add `from collections.abc import Mapping`.

```python
def mask(secret: str) -> str:
    """Hide a secret for display: '••••' plus the last 4 characters when it is long enough to spare them."""
    if not secret:
        return ""
    return "••••" + (secret[-4:] if len(secret) >= 12 else "")


def _env_key(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].removeprefix("export ").strip()


def update_env_text(text: str, updates: Mapping[str, str]) -> str:
    """Set KEY=value for each update where the key is defined, append the rest; every other line is kept."""
    if any("\n" in v or "\r" in v for v in updates.values()):
        raise ValueError("env values must be single-line")
    out: list[str] = []
    for line in text.splitlines():
        key = _env_key(line)
        if key in updates:
            prefix = "export " if line.strip().startswith("export ") else ""
            out.append(f"{prefix}{key}={updates[key]}")
        else:
            out.append(line)
    seen = {_env_key(line) for line in text.splitlines()}
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_settings_env.py -q`
Expected: `6 passed`

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

```bash
git add src/fenrir/settings.py tests/test_settings_env.py
git commit -m "feat(core): add env file update and secret masking

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Settings API

**Files:**
- Create: `src/fenrir/server/routes/settings.py`
- Modify: `src/fenrir/server/app.py` (register router)
- Test: `tests/server/test_settings_routes.py`

**Interfaces:**
- Consumes: Task 1 (`mask`, `update_env_text`); `fenrir.settings.load_settings(env_file: Path) -> Settings` and `Settings` (plan 01); `require_token` (plan 11).
- Produces: `GET /api/settings` → `SettingsView = {lse_api_key, nt_url, nt_token, holdout_months, safety_margin_pct, stale_seconds, heartbeat_policy}` with secrets masked, read fresh from `.env` (so it shows saved values before a restart); `PUT /api/settings` partial `SettingsView` → the new masked `SettingsView`, 422 on invalid or unknown fields.

- [ ] **Step 1: Write the failing tests**

```python
# tests/server/test_settings_routes.py
import os

import pytest
from fastapi.testclient import TestClient

from fenrir.server.app import create_app
from fenrir.settings import Settings

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("FENRIR_")]:
        monkeypatch.delenv(key)
    app = create_app(Settings(), token=TOKEN, static_dir=None)
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"X-Fenrir-Token": TOKEN})


def test_get_masks_secrets(client, tmp_path):
    (tmp_path / ".env").write_text("FENRIR_NT_TOKEN=abcdefghijkl1234\nFENRIR_LSE_API_KEY=short\n")
    body = client.get("/api/settings").json()
    assert body["nt_token"] == "••••1234"
    assert body["lse_api_key"] == "••••"
    assert "abcdefghijkl" not in str(body)


def test_put_writes_env_and_returns_masked_view(client, tmp_path):
    (tmp_path / ".env").write_text("# my notes\nFENRIR_NT_URL=http://old:8123\nOTHER=1\n")
    res = client.put("/api/settings", json={"nt_url": "http://10.0.0.5:8123", "nt_token": "secret-token-9876",
                                            "stale_seconds": 60, "heartbeat_policy": "flatten"})
    assert res.status_code == 200
    assert res.json()["nt_token"] == "••••9876"
    assert res.json()["stale_seconds"] == 60
    text = (tmp_path / ".env").read_text()
    assert text.startswith("# my notes\nFENRIR_NT_URL=http://10.0.0.5:8123\nOTHER=1\n")
    assert "FENRIR_NT_TOKEN=secret-token-9876" in text
    assert "FENRIR_HEARTBEAT_POLICY=flatten" in text


def test_put_creates_missing_env_file(client, tmp_path):
    assert client.put("/api/settings", json={"holdout_months": 6}).status_code == 200
    assert (tmp_path / ".env").read_text() == "FENRIR_HOLDOUT_MONTHS=6\n"


@pytest.mark.parametrize("body", [
    {"stale_seconds": 0},
    {"safety_margin_pct": 150},
    {"nt_token": "has space"},
    {"lse_api_key": 'quote"d'},
    {"nt_token": "a\nFENRIR_NT_URL=http://evil"},
    {"nt_url": "ftp://x"},
    {"heartbeat_policy": "panic"},
    {"data_dir": "/etc"},
])
def test_invalid_values_are_rejected_and_file_untouched(client, tmp_path, body):
    (tmp_path / ".env").write_text("FENRIR_NT_TOKEN=keep\n")
    assert client.put("/api/settings", json=body).status_code == 422
    assert (tmp_path / ".env").read_text() == "FENRIR_NT_TOKEN=keep\n"


def test_requires_token(client):
    assert TestClient(client.app, base_url="http://127.0.0.1:8000").get("/api/settings").status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_settings_routes.py -q`
Expected: FAIL (`404` responses; router not registered).

- [ ] **Step 3: Write the router**

```python
# src/fenrir/server/routes/settings.py
"""Read and edit `.env` settings. Secrets are masked; changes apply when Fenrir Desk restarts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fenrir.server.auth import require_token
from fenrir.settings import Settings, load_settings, mask, update_env_text

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_token)])

ENV_FILE = Path(".env")
EDITABLE = ("lse_api_key", "nt_url", "nt_token", "holdout_months", "safety_margin_pct", "stale_seconds", "heartbeat_policy")
SECRETS = {"lse_api_key", "nt_token"}
# No spaces, quotes, '#' or backslashes: values are written unquoted to .env.
SAFE = r"^[A-Za-z0-9_.:/@+=~%-]*$"


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lse_api_key: str | None = Field(None, pattern=SAFE)
    nt_url: str | None = Field(None, pattern=r"^https?://[A-Za-z0-9_.:/@+=~%-]+$")
    nt_token: str | None = Field(None, pattern=SAFE)
    holdout_months: int | None = Field(None, ge=1, le=120)
    safety_margin_pct: float | None = Field(None, gt=0, le=100)
    stale_seconds: int | None = Field(None, ge=1)
    heartbeat_policy: Literal["keep", "flatten"] | None = None


def _view(settings: Settings) -> dict[str, Any]:
    return {f: mask(getattr(settings, f)) if f in SECRETS else getattr(settings, f) for f in EDITABLE}


@router.get("")
def read() -> dict[str, Any]:
    return _view(load_settings(ENV_FILE))


@router.put("")
def write(body: SettingsUpdate) -> dict[str, Any]:
    updates = {f"FENRIR_{k.upper()}": str(v) for k, v in body.model_dump(exclude_none=True).items()}
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    ENV_FILE.write_text(update_env_text(text, updates))  # in place: Docker bind-mounts .env as a single file
    return _view(load_settings(ENV_FILE))
```

- [ ] **Step 4: Register the router**

In `src/fenrir/server/app.py`:

```python
from fenrir.server.routes import settings as settings_routes
```

```python
    app.include_router(settings_routes.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/server/test_settings_routes.py -q`
Expected: `12 passed`

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

```bash
git add src/fenrir/server/routes/settings.py src/fenrir/server/app.py tests/server/test_settings_routes.py
git commit -m "feat(server): add settings api that edits env with masked secrets

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Settings page

**Files:**
- Modify: `web/src/routes/settings.tsx` (replace the empty page component from plan 12)
- Create (by shadcn CLI, skipped if another plan already added them): `web/src/components/ui/{input,label,select}.tsx`

**Interfaces:**
- Consumes: `apiFetch<T>` (plan 12); `settingsRoute` in `web/src/routes/settings.tsx` (plan 12); Task 2 endpoints; shadcn `Button`, `Alert` (plan 12).
- Produces: the Settings page at `/settings`. Secret fields start empty and are only sent when typed into; other fields are sent when changed.

- [ ] **Step 0: Add the shadcn components this page needs**

Run: `cd web && npx shadcn@latest add input label select && cd ..`
Expected: the files exist in `web/src/components/ui/`. If the CLI says a file already exists (another plan added it), answer **No** to overwriting.

- [ ] **Step 1: Write the page**

Replace the contents of `web/src/routes/settings.tsx`, keeping plan 12's `createRoute` arguments (`getParentRoute`, `path`) as they are.

```tsx
// web/src/routes/settings.tsx
import { useState, type ReactNode } from 'react'
import { createRoute } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiFetch } from '../api'
import { rootRoute } from './root'

type SettingsView = {
  lse_api_key: string; nt_url: string; nt_token: string
  holdout_months: number; safety_margin_pct: number; stale_seconds: number
  heartbeat_policy: 'keep' | 'flatten'
}

function Section({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="grid gap-4 rounded-lg border p-4">
      <legend className="px-1 font-display font-semibold">{legend}</legend>
      {children}
    </fieldset>
  )
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  )
}

function SettingsForm({ current }: { current: SettingsView }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<SettingsView>({ ...current, lse_api_key: '', nt_token: '' })
  const save = useMutation({
    mutationFn: (body: Partial<SettingsView>) =>
      apiFetch<SettingsView>('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }),
    onSuccess: (data) => qc.setQueryData(['settings'], data),
  })
  const set = <K extends keyof SettingsView>(key: K, value: SettingsView[K]) => setDraft({ ...draft, [key]: value })
  const changes = Object.fromEntries(
    Object.entries(draft).filter(([k, v]) => v !== '' && v !== current[k as keyof SettingsView]),
  ) as Partial<SettingsView>

  return (
    <form className="max-w-2xl space-y-6" onSubmit={(e) => { e.preventDefault(); save.mutate(changes) }}>
      <Section legend="NinjaTrader">
        <Field id="nt-url" label="Address">
          <Input id="nt-url" value={draft.nt_url} onChange={(e) => set('nt_url', e.target.value)} />
        </Field>
        <Field id="nt-token" label="Token">
          <Input id="nt-token" type="password" autoComplete="off" placeholder={current.nt_token || 'not set'}
            value={draft.nt_token} onChange={(e) => set('nt_token', e.target.value)} />
        </Field>
        <Field id="heartbeat-policy" label="If the heartbeat is lost with a position open">
          <Select value={draft.heartbeat_policy} onValueChange={(v) => set('heartbeat_policy', v as SettingsView['heartbeat_policy'])}>
            <SelectTrigger id="heartbeat-policy" className="w-56"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="keep">Keep the bracket</SelectItem>
              <SelectItem value="flatten">Flatten</SelectItem>
            </SelectContent>
          </Select>
        </Field>
      </Section>
      <Section legend="London Strategic Edge">
        <Field id="lse-key" label="API key">
          <Input id="lse-key" type="password" autoComplete="off" placeholder={current.lse_api_key || 'not set'}
            value={draft.lse_api_key} onChange={(e) => set('lse_api_key', e.target.value)} />
        </Field>
        <Field id="stale-seconds" label="Pause entries after no bar for (seconds)">
          <Input id="stale-seconds" type="number" min={1} value={draft.stale_seconds}
            onChange={(e) => set('stale_seconds', Number(e.target.value))} />
        </Field>
      </Section>
      <Section legend="Backtests and safety">
        <Field id="holdout-months" label="Hold-out (months)">
          <Input id="holdout-months" type="number" min={1} max={120} value={draft.holdout_months}
            onChange={(e) => set('holdout_months', Number(e.target.value))} />
        </Field>
        <Field id="safety-margin" label="Halt the day at this % of the daily loss limit">
          <Input id="safety-margin" type="number" min={1} max={100} step="any" value={draft.safety_margin_pct}
            onChange={(e) => set('safety_margin_pct', Number(e.target.value))} />
        </Field>
      </Section>
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={save.isPending || Object.keys(changes).length === 0}>Save</Button>
        {save.isSuccess && <p role="status" className="text-sm text-muted-foreground">Saved. Restart Fenrir Desk to apply the changes.</p>}
        {save.isError && <p role="alert" className="text-sm text-destructive">{String(save.error)}</p>}
      </div>
    </form>
  )
}

function SettingsPage() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiFetch<SettingsView>('/api/settings') })
  return (
    <>
      <h1>Settings</h1>
      {settings.isError && <p role="alert" className="text-destructive">{String(settings.error)}</p>}
      {settings.data ? <SettingsForm current={settings.data} /> : !settings.isError && <p className="text-muted-foreground">Loading…</p>}
    </>
  )
}

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/settings',
  component: SettingsPage,
})
```

If plan 12 named the root route import differently, keep plan 12's import line.

- [ ] **Step 2: Typecheck**

Run: `npm --prefix web run typecheck`
Expected: exits 0, no errors.

- [ ] **Step 3: Check it by hand**

Run: `uv run fenrir serve` and open `http://127.0.0.1:8000/settings`.
Expected: fields show current values; token and key fields are empty with the masked value as placeholder; changing "Hold-out" to 6 and pressing Save shows "Saved. Restart Fenrir Desk to apply the changes." and `.env` contains `FENRIR_HOLDOUT_MONTHS=6`.

- [ ] **Step 4: Commit**

```bash
git add web/src/routes/settings.tsx web/src/components/ui web/package.json web/package-lock.json
git commit -m "feat(web): add settings page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Live page with kill switch

**Files:**
- Modify: `web/src/routes/live.tsx` (replace the empty page component from plan 12)
- Create (by shadcn CLI, skipped if another plan already added them): `web/src/components/ui/{alert-dialog,card,table,badge}.tsx`
- Test: `web/e2e/kill-switch.spec.ts`

**Interfaces:**
- Consumes: `apiFetch<T>` (plan 12); `liveRoute` in `web/src/routes/live.tsx` (plan 12); `GET /api/live/status`, `GET /api/live/journal?account=&day=`, `POST /api/live/kill`, `POST /api/live/resolve` (plans 21/22, shapes in the index).
- Produces: the Live page at `/live`. Button `Kill switch` opens a shadcn `AlertDialog` (`role=alertdialog`) with buttons `Flatten and disarm all` and `Cancel`; status text starting `Kill switch done`.

- [ ] **Step 0: Add the shadcn components this page needs**

Run: `cd web && npx shadcn@latest add alert-dialog card table badge && cd ..`
Expected: the files exist in `web/src/components/ui/`. If the CLI says a file already exists (another plan added it), answer **No** to overwriting.

- [ ] **Step 1: Write the failing Playwright test**

The API is mocked with `page.route`; the kill endpoint's behavior is tested in plan 22.

```ts
// web/e2e/kill-switch.spec.ts
import { expect, test, type Page } from '@playwright/test'

const STATUS = {
  lse: { connected: true, last_bar_ts: '2026-10-12T14:30:00Z' },
  nt: { connected: true, last_heartbeat: '2026-10-12T14:30:58Z' },
  accounts: [{
    account: 'LUCID-1', mode: 'live', strategy: 'orb', rules_account: 'lucid-flex-50k', contract: 'MNQ',
    halted: false, halt_reason: null, balance: 50250,
    limits: { drawdown_remaining: 1750, daily_loss_remaining: null, max_minis: 4, held_minis: 0.2 },
    position: { qty: 2, entry: 20100.25, stop: 20090.25, target: 20120.25 },
  }],
}

async function mockApi(page: Page, statusCode: number) {
  const kills: number[] = []
  await page.route('**/api/live/status', (r) =>
    statusCode === 200 ? r.fulfill({ json: STATUS }) : r.fulfill({ status: statusCode, body: 'runner down' }))
  await page.route(/\/api\/live\/journal/, (r) => r.fulfill({ json: [] }))
  await page.route('**/api/live/kill', (r) => { kills.push(1); return r.fulfill({ json: { ok: true } }) })
  return kills
}

test('kill switch asks for confirmation, then flattens and disarms everything', async ({ page }) => {
  const kills = await mockApi(page, 200)
  await page.goto('/live')
  await expect(page.getByText('Drawdown remaining')).toBeVisible()
  await page.getByRole('button', { name: 'Kill switch' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Flatten and disarm all' }).click()
  await expect(page.getByText(/^Kill switch done/)).toBeVisible()
  expect(kills).toHaveLength(1)
})

test('dismissing the confirmation sends nothing', async ({ page }) => {
  const kills = await mockApi(page, 200)
  await page.goto('/live')
  await page.getByRole('button', { name: 'Kill switch' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByRole('alertdialog')).toHaveCount(0)
  await expect(page.getByText(/^Kill switch done/)).toHaveCount(0)
  expect(kills).toHaveLength(0)
})

test('kill switch works while status is unavailable', async ({ page }) => {
  const kills = await mockApi(page, 500)
  await page.goto('/live')
  await page.getByRole('button', { name: 'Kill switch' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Flatten and disarm all' }).click()
  await expect(page.getByText(/^Kill switch done/)).toBeVisible()
  expect(kills).toHaveLength(1)
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && npx playwright test e2e/kill-switch.spec.ts`
Expected: `3 failed` (no button named `Kill switch`).

- [ ] **Step 3: Write the page**

Replace the contents of `web/src/routes/live.tsx`, keeping plan 12's `createRoute` arguments as they are.

```tsx
// web/src/routes/live.tsx
import { createRoute } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { apiFetch } from '../api'
import { rootRoute } from './root'

type AccountStatus = {
  account: string; mode: 'dry' | 'sim' | 'live'; strategy: string; rules_account: string; contract: string
  halted: boolean; halt_reason: string | null; balance: number
  limits: { drawdown_remaining: number; daily_loss_remaining: number | null; max_minis: number; held_minis: number }
  position: { qty: number; entry: number | null; stop: number | null; target: number | null }
}
type LiveStatus = {
  lse: { connected: boolean; last_bar_ts: string | null }
  nt: { connected: boolean; last_heartbeat: string | null }
  accounts: AccountStatus[]
}
type JournalRow = { id: number; ts: string; account: string; kind: string; payload: unknown }

const usd = (x: number | null) => (x === null ? 'no limit' : `$${x.toFixed(2)}`)

/** CME trading day (YYYY-MM-DD): after 18:00 New York time it is already the next day's session. */
function tradingDay(now: Date): string {
  const ny = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }))
  if (ny.getHours() >= 18) ny.setDate(ny.getDate() + 1)
  return `${ny.getFullYear()}-${String(ny.getMonth() + 1).padStart(2, '0')}-${String(ny.getDate()).padStart(2, '0')}`
}

const post = <T,>(path: string, body?: unknown) =>
  apiFetch<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) })

function KillSwitch() {
  const qc = useQueryClient()
  const kill = useMutation({
    mutationFn: () => post<{ ok: boolean }>('/api/live/kill'),
    onSettled: () => Promise.all([qc.invalidateQueries({ queryKey: ['live'] }), qc.invalidateQueries({ queryKey: ['arming'] })]),
  })
  return (
    <section aria-label="Kill switch" className="flex items-center gap-3">
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button variant="destructive" size="lg" disabled={kill.isPending}>Kill switch</Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop everything?</AlertDialogTitle>
            <AlertDialogDescription>Flatten every position, cancel every order and disarm every account.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90" onClick={() => kill.mutate()}>
              Flatten and disarm all
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {kill.data?.ok && <p role="status" className="text-sm text-muted-foreground">Kill switch done: flattened, cancelled and disarmed.</p>}
      {(kill.isError || kill.data?.ok === false) && (
        <p role="alert" className="text-sm text-destructive">
          Kill switch failed{kill.error ? `: ${String(kill.error)}` : ''}. Flatten in NinjaTrader now.
        </p>
      )}
    </section>
  )
}

function Journal({ account }: { account: string }) {
  const day = tradingDay(new Date())
  const rows = useQuery({
    queryKey: ['live', 'journal', account, day],
    queryFn: () => apiFetch<JournalRow[]>(`/api/live/journal?account=${encodeURIComponent(account)}&day=${day}`),
    refetchInterval: 5000,
  })
  // shortcut: shows the latest 50 rows only; add paging if the user needs the full day here (Claude can read SQLite).
  const latest = (rows.data ?? []).slice(-50)
  return (
    <Table>
      <TableCaption className="caption-top text-left">Journal for {day} (latest 50)</TableCaption>
      <TableHeader>
        <TableRow><TableHead>Time</TableHead><TableHead>Kind</TableHead><TableHead>Details</TableHead></TableRow>
      </TableHeader>
      <TableBody>
        {latest.map((r) => (
          <TableRow key={r.id}>
            <TableCell className="whitespace-nowrap">{r.ts}</TableCell>
            <TableCell><Badge variant={r.kind === 'alert' || r.kind === 'halt' || r.kind === 'rejection' ? 'destructive' : 'secondary'}>{r.kind}</Badge></TableCell>
            <TableCell><code className="text-xs">{JSON.stringify(r.payload)}</code></TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function AccountCard({ a }: { a: AccountStatus }) {
  const qc = useQueryClient()
  const resolve = useMutation({
    mutationFn: () => post<{ ok: boolean }>('/api/live/resolve', { account: a.account }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['live'] }),
  })
  const p = a.position
  return (
    <Card role="region" aria-label={a.account}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {a.account} <Badge variant={a.mode === 'live' ? 'destructive' : 'secondary'}>{a.mode}</Badge>
        </CardTitle>
        <CardDescription>{a.strategy} · {a.rules_account} · {a.contract}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {a.halted && (
          <Alert variant="destructive">
            <AlertTitle>Halted</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center gap-3">
              <span>{a.halt_reason}</span>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button variant="outline" size="sm">Resolve</Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Resume {a.account}?</AlertDialogTitle>
                    <AlertDialogDescription>Only do this after matching NinjaTrader's positions and orders by hand.</AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={() => resolve.mutate()}>Resume</AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </AlertDescription>
          </Alert>
        )}
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Balance</dt><dd>{usd(a.balance)}</dd>
          <dt className="text-muted-foreground">Drawdown remaining</dt><dd>{usd(a.limits.drawdown_remaining)}</dd>
          <dt className="text-muted-foreground">Daily loss remaining</dt><dd>{usd(a.limits.daily_loss_remaining)}</dd>
          <dt className="text-muted-foreground">Contracts</dt><dd>{a.limits.held_minis} of {a.limits.max_minis} minis</dd>
          <dt className="text-muted-foreground">Position</dt>
          <dd>{p.qty === 0 ? 'Flat' : `${p.qty} @ ${p.entry} · stop ${p.stop} · target ${p.target}`}</dd>
        </dl>
        <Journal account={a.account} />
      </CardContent>
    </Card>
  )
}

function LivePage() {
  const status = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => apiFetch<LiveStatus>('/api/live/status'),
    refetchInterval: 2000,
  })
  const s = status.data
  return (
    <>
      <h1>Live</h1>
      <div className="space-y-6">
        <KillSwitch />
        {status.isError && <p role="alert" className="text-destructive">Status unavailable: {String(status.error)}</p>}
        {s && (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
            <dt className="text-muted-foreground">LSE</dt>
            <dd className="flex items-center gap-2">
              <Badge variant={s.lse.connected ? 'secondary' : 'destructive'}>{s.lse.connected ? 'Connected' : 'Disconnected'}</Badge>
              last bar {s.lse.last_bar_ts ?? 'none'}
            </dd>
            <dt className="text-muted-foreground">NinjaTrader</dt>
            <dd className="flex items-center gap-2">
              <Badge variant={s.nt.connected ? 'secondary' : 'destructive'}>{s.nt.connected ? 'Connected' : 'Disconnected'}</Badge>
              last heartbeat {s.nt.last_heartbeat ?? 'none'}
            </dd>
          </dl>
        )}
        {s?.accounts.length === 0 && <p className="text-muted-foreground">No account is armed.</p>}
        {s?.accounts.map((a) => <AccountCard key={a.account} a={a} />)}
      </div>
    </>
  )
}

export const liveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live',
  component: LivePage,
})
```

If plan 12 named the root route import differently, keep plan 12's import line.

- [ ] **Step 4: Typecheck and run the test**

Run: `npm --prefix web run typecheck`
Expected: exits 0, no errors.

Run: `cd web && npx playwright test e2e/kill-switch.spec.ts`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add web/src/routes/live.tsx web/src/components/ui web/package.json web/package-lock.json web/e2e/kill-switch.spec.ts
git commit -m "feat(web): add live page with limits, journal and kill switch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
