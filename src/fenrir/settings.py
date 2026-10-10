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


def parse_env_text(text: str) -> dict[str, str]:
    pairs = (line.strip().split("=", 1) for line in text.splitlines())
    return {
        k.strip().removeprefix("export ").strip(): v.strip().strip('"').strip("'")
        for k, v in (p for p in pairs if len(p) == 2 and not p[0].startswith("#"))
    }


def load_settings(env_file: Path = Path(".env")) -> Settings:
    from_file = parse_env_text(env_file.read_text()) if env_file.exists() else {}
    return parse_settings({**from_file, **os.environ})
