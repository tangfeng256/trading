from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def as_eastern(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=EASTERN)
    return timestamp.astimezone(EASTERN)


def in_time_window(timestamp: datetime, start: str, end: str) -> bool:
    value = as_eastern(timestamp).time()
    return parse_time(start) <= value <= parse_time(end)


def bps(part: float, whole: float) -> float:
    return 0.0 if whole == 0 else (part / whole) * 10_000.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
