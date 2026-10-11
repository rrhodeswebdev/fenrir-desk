import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


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


def _policy(value: str) -> str:
    if value not in ("keep", "flatten"):
        raise ValueError(f"must be 'keep' or 'flatten', got {value!r}")
    return value


# field name -> (env key, converter)
_FIELDS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "lse_api_key": ("FENRIR_LSE_API_KEY", str),
    "nt_url": ("FENRIR_NT_URL", str),
    "nt_token": ("FENRIR_NT_TOKEN", str),
    "holdout_months": ("FENRIR_HOLDOUT_MONTHS", int),
    "safety_margin_pct": ("FENRIR_SAFETY_MARGIN_PCT", float),
    "stale_seconds": ("FENRIR_STALE_SECONDS", int),
    "heartbeat_policy": ("FENRIR_HEARTBEAT_POLICY", _policy),
    "data_dir": ("FENRIR_DATA_DIR", Path),
    "rules_dir": ("FENRIR_RULES_DIR", Path),
    "strategies_dir": ("FENRIR_STRATEGIES_DIR", Path),
}


def _convert(key: str, convert: Callable[[str], Any], raw: str) -> Any:
    try:
        return convert(raw)
    except ValueError as e:
        raise ValueError(f"{key}: {e}") from None


def parse_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        **{
            name: _convert(key, convert, env[key])
            for name, (key, convert) in _FIELDS.items()
            if env.get(key, "") != ""
        }
    )


def _env_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #", 1)[0].strip()  # unquoted values may carry an inline comment


def parse_env_text(text: str) -> dict[str, str]:
    pairs = (line.strip().split("=", 1) for line in text.splitlines())
    return {
        k.strip().removeprefix("export ").strip(): _env_value(v)
        for k, v in (p for p in pairs if len(p) == 2 and not p[0].startswith("#"))
    }


def _env_key(line: str) -> str | None:
    k, sep, _ = line.strip().partition("=")
    return k.strip().removeprefix("export ").strip() if sep and not k.startswith("#") else None


def _env_line(key: str, value: str) -> str:
    if value.splitlines() not in ([], [value]):
        raise ValueError(f"{key}: value must be a single line")
    # parse_env_text strips one pair of matching quotes, so quoting always round-trips
    return f'{key}="{value}"' if _env_value(value) != value else f"{key}={value}"


def _replace_line(line: str, updates: Mapping[str, str]) -> str:
    key = _env_key(line)
    if key not in updates:
        return line
    return _env_line(key, updates[key]) + line[len(line.rstrip("\r\n")) :]


def update_env_text(text: str, updates: Mapping[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    present = {_env_key(line) for line in lines}
    out = "".join(_replace_line(line, updates) for line in lines)
    added = "".join(_env_line(k, v) + "\n" for k, v in updates.items() if k not in present)
    return out + ("\n" if added and out and not out.endswith("\n") else "") + added


def mask_secret(value: str) -> str:
    # last 4 only once that is at most a third of the secret
    return "" if not value else "••••" + value[-4:] if len(value) >= 12 else "••••"


def load_settings(env_file: Path = Path(".env")) -> Settings:
    from_file = parse_env_text(env_file.read_text()) if env_file.exists() else {}
    # an empty env var (e.g. compose passing through an unset one) must not hide the file's value
    from_env = {k: v for k, v in os.environ.items() if v != ""}
    return parse_settings({**from_file, **from_env})
