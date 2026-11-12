"""Time and duration helpers: accept friendly inputs, emit Flux literals."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$")

TimeLike = datetime | str


def parse_duration(value: str | timedelta) -> timedelta:
    """``"15m"``, ``"24h"``, ``"7d"``, ``"2w"`` (or a timedelta) to a timedelta."""
    if isinstance(value, timedelta):
        return value
    m = _DUR.match(value)
    if not m:
        raise ValueError(
            f"Can't read duration {value!r}; use a number and s/m/h/d/w, like '10m' or '24h'."
        )
    return timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])


def to_utc(value: TimeLike) -> datetime:
    """A datetime (naive means local time) or ISO-8601 string, in UTC."""
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        value = datetime.fromisoformat(s)
    if value.tzinfo is None:
        value = value.astimezone()  # naive: the machine's local time
    return value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def resolve_range(
    start: TimeLike | None,
    end: TimeLike | None,
    last: str | timedelta | None,
    *,
    now: datetime | None = None,
    default_last: str = "24h",
) -> tuple[datetime, datetime]:
    """Pick [start, end) from either ``last=`` or ``start=``/``end=``."""
    now = now or now_utc()
    if last is not None and start is not None:
        raise ValueError("Pass either last=... or start=..., not both.")
    stop = to_utc(end) if end is not None else now
    if start is not None:
        begin = to_utc(start)
    else:
        begin = stop - parse_duration(last if last is not None else default_last)
    if begin >= stop:
        raise ValueError("start must be before end.")
    return begin, stop


def rfc3339(t: datetime) -> str:
    """UTC timestamp Flux accepts, e.g. ``2026-01-02T03:04:05Z``."""
    t = t.astimezone(timezone.utc)
    if t.microsecond:
        return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def flux_duration(d: timedelta) -> str:
    """Shortest exact Flux duration literal: ``1d``, ``6h``, ``15m``, ``30s``."""
    secs = int(d.total_seconds())
    if secs <= 0:
        raise ValueError("Duration must be positive.")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def parse_time(value: str) -> datetime | None:
    """An Influx ``_time`` cell to an aware UTC datetime (None if unreadable)."""
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Influx may emit nanoseconds; Python handles at most microseconds.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m and m.group(2) and len(m.group(2)) > 7:
        s = m.group(1) + m.group(2)[:7] + m.group(3)
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)
