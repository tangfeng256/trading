from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def ensure_aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def to_et(ts: datetime) -> datetime:
    return ensure_aware(ts).astimezone(ET)


def parse_hms(value: str) -> time:
    return time.fromisoformat(value)


def in_time_window(ts: datetime, start_hms: str, end_hms: str) -> bool:
    local = to_et(ts).time()
    return parse_hms(start_hms) <= local <= parse_hms(end_hms)


def iso(ts: datetime) -> str:
    return ensure_aware(ts).isoformat()
