from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _require_aware(ts: datetime) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"naive datetime: {ts!r}")


def trading_day(ts: datetime) -> date:
    _require_aware(ts)
    return (ts.astimezone(NY) + timedelta(hours=6)).date()


def at_or_after(ts: datetime, hhmm: str, tz: str = "America/New_York") -> bool:
    _require_aware(ts)
    zone = ZoneInfo(tz)
    # CME Globex: trading day D runs 18:00 ET on D-1 to 17:00 ET on D, whatever tz hhmm is in
    session_open = datetime.combine(trading_day(ts) - timedelta(days=1), time(18), NY)
    hour, minute = (int(p) for p in hhmm.split(":"))
    mark = datetime.combine(session_open.astimezone(zone).date(), time(hour, minute), zone)
    if mark < session_open:
        mark = datetime.combine(mark.date() + timedelta(days=1), time(hour, minute), zone)
    return ts >= mark
