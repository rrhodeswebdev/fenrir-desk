import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl

from fenrir.core.bars import Bar
from fenrir.core.instruments import Instruments, front_month, in_roll_window
from fenrir.data.pull import Window

SCHEMA = {
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}
SERIES = ("ES", "NQ")  # price series with bars; MES and MNQ use ES and NQ prices


@dataclass(frozen=True, slots=True)
class Coverage:
    series: str
    first_day: date | None
    last_day: date | None
    days: int
    gaps: tuple[
        tuple[date, date], ...
    ]  # missing trading-day ranges (weekends/CME holidays excluded)


# --- pure ---


def trading_day_expr() -> pl.Expr:
    # same rule as fenrir.core.sessions.trading_day: 18:00 New York starts the next trading day
    return (pl.col("ts").dt.convert_time_zone("America/New_York") + pl.duration(hours=6)).dt.date()


def find_gaps(present: set[date], expected: Iterable[date]) -> tuple[tuple[date, date], ...]:
    gaps: list[tuple[date, date]] = []
    run: tuple[date, date] | None = None
    for d in expected:
        if d in present:
            if run:
                gaps.append(run)
            run = None
        else:
            run = (run[0], d) if run else (d, d)
    return tuple(gaps + ([run] if run else []))


def with_contract(
    manifest: Mapping[str, Any], window: Window, rows: int, today: date, pulled_at: str
) -> dict[str, Any]:
    entry = {
        "first_day": window.first.isoformat(),
        "last_day": window.last.isoformat(),
        "rows": rows,
        # yesterday's evening session may still be publishing; only older windows are final
        "complete": window.last < today - timedelta(days=1),
    }
    series = manifest.get("series", {})
    return {
        **manifest,
        "pulled_at": pulled_at,
        "series": {
            **series,
            window.series: {**series.get(window.series, {}), window.contract: entry},
        },
    }


# --- shell ---


def _series_dir(data_dir: Path, series: str) -> Path:
    return data_dir / "bars" / series / "1m"


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / "bars" / "manifest.json"


# every call _atomic_write makes with dir_fd; native Windows has none (and no hostile user)
_DIR_FD = {os.open, os.mkdir, os.rename, os.unlink} <= os.supports_dir_fd
MAX_MANIFEST = 16 * 2**20


def _open_dirs(data_dir: Path, parts: Sequence[str]) -> int:
    """A fd for data_dir/parts..., creating missing ones; each is opened O_NOFOLLOW, so a link
    there, even one swapped in a moment ago, fails with ELOOP instead of being followed."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(data_dir, flags)
    try:
        for part in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=fd)
            fd, parent = os.open(part, flags, dir_fd=fd), fd
            os.close(parent)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _atomic_write(data_dir: Path, path: Path, write: Callable[[BinaryIO], Any]) -> None:
    """`write` fills a fresh temp file beside `path`, which then replaces `path`.

    data/ is terminal-writable in the image and the pull writes as root (DEC-1, #135):
    ValueError, writing nothing, when `data_dir` or a directory below it on the way to `path` is
    a symlink. A symlink at `path` itself is replaced, never written through.
    """
    if not _DIR_FD:
        return _atomic_write_checked(data_dir, path, write)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = _open_dirs(data_dir, path.relative_to(data_dir).parts[:-1])
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"refusing to write through a symlink on the way to {path}") from None
        raise
    try:
        # a random name opened O_EXCL|O_NOFOLLOW: a planted <path>.tmp link is never followed
        tmp = f"{path.name}.{secrets.token_hex(8)}.tmp"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as f:
                write(f)
            os.replace(tmp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            os.unlink(tmp, dir_fd=fd)
            raise
    finally:
        os.close(fd)


def _atomic_write_checked(data_dir: Path, path: Path, write: Callable[[BinaryIO], Any]) -> None:
    """`_atomic_write` where dir_fd is missing (native Windows): checks, then writes."""
    # shortcut: a link swapped in between check and write is followed; fine with no hostile user
    rel = path.relative_to(data_dir)
    for p in [data_dir / Path(*rel.parts[:i]) for i in range(len(rel.parts) + 1)]:
        if p.is_symlink():
            raise ValueError(f"refusing to write through symlink {p}")
        if p != path:
            p.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if hasattr(os, "fchmod"):  # keep the 0644 a plain write gave; mkstemp is 0600
                os.fchmod(f.fileno(), 0o644)
            write(f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_manifest(data_dir: Path) -> dict[str, Any]:
    """ValueError for a symlink, a non-regular file or one over 16 MB: the pull reads this as root
    in the image, where the terminal could plant a link, a FIFO or /dev/zero (DEC-1)."""
    path = _manifest_path(data_dir)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {"series": {}}
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"refusing to read symlink {path}") from None
        raise
    with os.fdopen(fd, "rb") as f:
        text = f.read(MAX_MANIFEST + 1) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    if text is None or len(text) > MAX_MANIFEST:
        raise ValueError(f"refusing to read {path}: not a regular file under 16 MB")
    return json.loads(text)


def write_manifest(data_dir: Path, manifest: Mapping[str, Any]) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True)
    _atomic_write(data_dir, _manifest_path(data_dir), lambda f: f.write(text.encode()))


def data_version(data_dir: Path) -> str:
    path = _manifest_path(data_dir)
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def write_contract(data_dir: Path, series: str, contract: str, frame: pl.DataFrame) -> int:
    clean = (
        frame.select(
            pl.col("ts").dt.convert_time_zone("UTC").dt.cast_time_unit("us"),
            *(pl.col(c).cast(t) for c, t in SCHEMA.items() if c != "ts"),
        )
        .unique("ts", keep="last", maintain_order=True)
        .sort("ts")
    )
    path = _series_dir(data_dir, series) / f"{contract}.parquet"
    _atomic_write(data_dir, path, clean.write_parquet)
    return clean.height


def write_bars(data_dir: Path, series: str, contract: str, bars: Sequence[Bar]) -> None:
    if not bars:
        return
    new = pl.DataFrame(
        {c: [getattr(b, c) for b in bars] for c in SCHEMA},
        schema=SCHEMA,
    )
    path = _series_dir(data_dir, series) / f"{contract}.parquet"
    merged = pl.concat([pl.read_parquet(path), new]) if path.exists() else new
    rows = write_contract(data_dir, series, contract, merged)  # keep="last": new bars win
    days = merged.select(trading_day_expr()).to_series()
    window = Window(series, contract, days.min(), days.max())
    # today=date.max marks the contract complete, so a later `fenrir data pull` leaves it alone
    manifest = with_contract(
        read_manifest(data_dir), window, rows, date.max, datetime.now(UTC).isoformat()
    )
    write_manifest(data_dir, manifest)


def _read_series(
    data_dir: Path, series: str, columns: list[str] | None = None
) -> pl.DataFrame | None:
    files = sorted(_series_dir(data_dir, series).glob("*.parquet"))
    if not files:
        return None
    return pl.concat(
        [
            pl.read_parquet(f, columns=columns).with_columns(pl.lit(f.stem).alias("contract"))
            for f in files
        ]
    ).with_columns(trading_day_expr().alias("day"))


def load_bars(
    data_dir: Path,
    instruments: Instruments,
    series: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[Bar, ...]:
    frame = _read_series(data_dir, series)
    if frame is None:
        return ()
    if start:
        frame = frame.filter(pl.col("day") >= start)
    if end:
        frame = frame.filter(pl.col("day") <= end)
    # roll-window days get no front row, so the join drops them (docs/data-gate.md "Roll mismatch")
    days = [d for d in frame["day"].unique().to_list() if not in_roll_window(d)]
    front = pl.DataFrame(
        {"day": days, "contract": [front_month(instruments, series, d) for d in days]},
        schema={"day": pl.Date, "contract": pl.String},
    )
    rows = frame.join(front, on=["day", "contract"]).sort("ts")
    return tuple(
        Bar(ts, o, h, lo, c, v, k)
        for ts, o, h, lo, c, v, k in rows.select(
            "ts", "open", "high", "low", "close", "volume", "contract"
        ).iter_rows()
    )


def expected_days(first: date, last: date) -> Sequence[date]:
    import pandas_market_calendars as mcal

    return [d.date() for d in mcal.get_calendar("CME_Equity").valid_days(first, last)]


def coverage(data_dir: Path, series: str) -> Coverage:
    # only the timestamps: the Data page polls this every 2 s during a pull
    frame = _read_series(data_dir, series, columns=["ts"])
    days = (
        set()
        if frame is None
        else {d for d in frame["day"].unique().to_list() if not in_roll_window(d)}
    )
    if not days:
        return Coverage(series, None, None, 0, ())
    first, last = min(days), max(days)
    expected = [d for d in expected_days(first, last) if not in_roll_window(d)]
    return Coverage(series, first, last, len(days), find_gaps(days, expected))
