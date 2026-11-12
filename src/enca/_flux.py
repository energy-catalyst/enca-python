"""Internal: Flux building, the API's word blocklist, tiers, annotated CSV.

Nothing here is public API. The enca-api ``/query`` endpoint runs Flux behind
per-user preludes (``telemetry``, ``telemetry_1m``, ``telemetry_10m``,
``telemetry_1h``; each takes ``start``/``stop``) and refuses any query that
contains certain words. Every query this library builds is checked with
:func:`check_blocklist` before it is sent.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from ._times import flux_duration, rfc3339
from .errors import QueryError

#: Words enca-api refuses anywhere in a query (comments and strings included).
BLOCKED_WORDS = frozenset({"from", "import", "to", "buckets", "cardinality", "wideTo"})
_BLOCKED_LOWER = frozenset(w.lower() for w in BLOCKED_WORDS)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

RAW = "telemetry"
TIER_1M = "telemetry_1m"
TIER_10M = "telemetry_10m"
TIER_1H = "telemetry_1h"

#: Window length of each downsampled tier (raw has none).
TIER_WINDOW = {
    TIER_1M: timedelta(minutes=1),
    TIER_10M: timedelta(minutes=10),
    TIER_1H: timedelta(hours=1),
}

_STEPS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
)

#: Upper bound on points per series in one history call; keeps responses far
#: below the API's 20 MB result limit.
MAX_POINTS = 20_000


def check_blocklist(flux: str) -> None:
    """Raise :class:`QueryError` if ``flux`` contains a word the API blocks."""
    bad = sorted({w for w in _WORD.findall(flux) if w.lower() in _BLOCKED_LOWER})
    if bad:
        raise QueryError(
            "The API refuses queries containing the word(s) "
            + ", ".join(repr(w) for w in bad)
            + " (anywhere, comments and strings included). Use the telemetry*() "
            "functions instead of from()/buckets(), and rename or remove those words."
        )


def tier_for(start: datetime, stop: datetime, now: datetime) -> str:
    """Pick the prelude tier for [start, stop), same rule as the app.

    * raw ``telemetry``: spans of up to 3 h that end within 5 min of now;
    * ``telemetry_1m``: up to 50 h;
    * ``telemetry_10m``: up to 35 days;
    * ``telemetry_1h``: beyond.
    """
    span = stop - start
    if span <= timedelta(hours=3) and now - stop <= timedelta(minutes=5):
        return RAW
    if span <= timedelta(hours=50):
        return TIER_1M
    if span <= timedelta(days=35):
        return TIER_10M
    return TIER_1H


def step_for(span: timedelta) -> timedelta:
    """Finest default bucket keeping a series at about 200 points."""
    for s in _STEPS:
        if span / s <= 200:
            return s
    return _STEPS[-1]


def tier_for_step(start: datetime, stop: datetime, now: datetime, every: timedelta) -> str:
    """:func:`tier_for`, stepped down to a finer tier when ``every`` is finer
    than the chosen tier's window (each bucket must hold whole tier windows)."""
    tier = tier_for(start, stop, now)
    if tier == RAW:
        return tier
    for t in (TIER_1H, TIER_10M, TIER_1M):
        if (
            TIER_WINDOW[t] <= TIER_WINDOW[tier]
            and every >= TIER_WINDOW[t]
            and _divides(TIER_WINDOW[t], every)
        ):
            return t
    if stop - start <= timedelta(hours=3) and now - stop <= timedelta(minutes=5):
        return RAW
    raise QueryError(
        f"every={flux_duration(every)} is finer than the stored data allows for this range; "
        "use at least 1m (or a range within the last 3 hours)."
    )


def _divides(window: timedelta, every: timedelta) -> bool:
    return int(every.total_seconds()) % int(window.total_seconds()) == 0


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_SAFE_ENTITY = re.compile(r"^[A-Za-z0-9_./\-]+$")


def safe_id(value: str) -> str:
    """A site or device id, refused if it could break out of a Flux string."""
    if not _SAFE_ID.match(value):
        raise ValueError(f"Not a valid site/device id: {value!r}")
    return value


def safe_entity(value: str) -> str:
    if not _SAFE_ENTITY.match(value):
        raise ValueError(f"Not a valid entity: {value!r}")
    return value


def _field(tier: str) -> str:
    return "value" if tier == RAW else "mean"


def _or(var: str, values: Iterable[str]) -> str:
    return " or ".join(f'r.{var} == "{v}"' for v in values)


def series_flux(
    *,
    tier: str,
    site: str,
    device: str,
    entities: Sequence[str],
    start: datetime,
    stop: datetime,
    every: timedelta,
) -> str:
    """Bucket means of ``entities`` of one device (the app's history query)."""
    if not entities:
        raise ValueError("No entities to query.")
    raw = tier == RAW
    which = _or("entity", (safe_entity(e) for e in entities))
    flux = (
        f"{tier}(start: {rfc3339(start)}, stop: {rfc3339(stop)})\n"
        '  |> filter(fn: (r) => r._measurement == "mqtt_consumer")\n'
        f'  |> filter(fn: (r) => r._field == "{_field(tier)}")\n'
        f'  |> filter(fn: (r) => r.site == "{safe_id(site)}"'
        f' and r.appliance == "{safe_id(device)}")\n'
        f"  |> filter(fn: (r) => {which})\n"
        + ("  |> toFloat()\n" if raw else "")
        + f"  |> aggregateWindow(every: {flux_duration(every)}, fn: mean, createEmpty: false)\n"
        '  |> keep(columns: ["entity", "_time", "_value"])\n'
    )
    check_blocklist(flux)
    return flux


#: Entities of live readings outside ``data/`` that snapshots use.
EXTRA_LIVE_ENTITIES = ("bus/inverter/data/compressor_running_speed",)


def latest_flux(
    *, site: str, devices: Sequence[str], start: datetime, stop: datetime, config: bool = False
) -> str:
    """The last raw value of every live (or, with ``config``, every
    ``info/``/``assign/``) entity of ``devices`` within [start, stop)."""
    if not devices:
        raise ValueError("No devices to query.")
    apps = _or("appliance", (safe_id(d) for d in devices))
    if config:
        which = "r.entity =~ /^(info|assign)\\// and r.entity !~ /^info\\/components\\//"
    else:
        extra = "".join(f' or r.entity == "{e}"' for e in EXTRA_LIVE_ENTITIES)
        which = f"r.entity =~ /^(data|remote)\\//{extra}"
    flux = (
        f"{RAW}(start: {rfc3339(start)}, stop: {rfc3339(stop)})\n"
        '  |> filter(fn: (r) => r._measurement == "mqtt_consumer")\n'
        '  |> filter(fn: (r) => r._field == "value")\n'
        f'  |> filter(fn: (r) => r.site == "{safe_id(site)}" and ({apps}))\n'
        f"  |> filter(fn: (r) => {which})\n"
        '  |> group(columns: ["appliance", "entity"])\n'
        "  |> last()\n"
        "  |> group()\n"
        '  |> keep(columns: ["appliance", "entity", "_value", "_time"])\n'
    )
    check_blocklist(flux)
    return flux


COUNTER_ENTITIES = (
    "energy/electric_wh",
    "energy/electric_heating_wh",
    "energy/electric_cooling_wh",
    "energy/heating_wh",
    "energy/cooling_wh",
    "energy/ground_wh",
)


def energy_hours_flux(*, site: str, device: str, start: datetime, stop: datetime) -> str:
    """Hourly inputs for energy over [start, stop) of one heat pump.

    Four reads, tagged by ``yield`` name (column ``result``):

    * ``counter``: each energy counter's last (max) value per hour, 1h tier,
      from one hour early so the first hour has a baseline;
    * ``power``: total_power's 1-minute means summed per hour (W·min);
    * ``cop``: COP's mean per hour (10m tier);
    * ``mode``: the mode's last value per hour (10m tier), from six hours
      early so the running mode carries into the first hours.
    """
    s, d = safe_id(site), safe_id(device)
    head = (
        '  |> filter(fn: (r) => r._measurement == "mqtt_consumer"'
        f' and r.site == "{s}" and r.appliance == "{d}")\n'
    )

    def read(tier: str, begin: datetime, what: str, fn: str, name: str) -> str:
        return (
            f"{tier}(start: {rfc3339(begin)}, stop: {rfc3339(stop)})\n"
            + head
            + f"  |> filter(fn: (r) => {what})\n"
            f'  |> aggregateWindow(every: 1h, fn: {fn}, createEmpty: false, timeSrc: "_start")\n'
            '  |> keep(columns: ["entity", "_field", "_time", "_value"])\n'
            f'  |> yield(name: "{name}")\n'
        )

    counters = _or("entity", COUNTER_ENTITIES)
    flux = (
        read(
            TIER_1H,
            start - timedelta(hours=1),
            f'({counters}) and r._field == "max"',
            "max",
            "counter",
        )
        + read(
            TIER_1M, start, 'r.entity == "data/total_power" and r._field == "mean"', "sum", "power"
        )
        + read(TIER_10M, start, 'r.entity == "data/cop" and r._field == "mean"', "mean", "cop")
        + read(
            TIER_10M,
            start - timedelta(hours=6),
            'r.entity == "data/mode" and r._field == "last"',
            "last",
            "mode",
        )
    )
    check_blocklist(flux)
    return flux


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse Influx annotated CSV into rows of column → cell.

    Annotation lines (``#...``) are skipped; a blank line starts a new table
    with its own header. The leading unnamed column is dropped. An error
    table (``error,reference``) raises :class:`QueryError`.
    """
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for record in csv.reader(io.StringIO(text)):
        if not record or all(c == "" for c in record):
            header = None
            continue
        if record[0].startswith("#"):
            header = None  # annotations precede a table's header row
            continue
        if header is None:
            header = record
            continue
        row = {k: v for k, v in zip(header, record, strict=False) if k}
        if "error" in row and "_value" not in header:
            raise QueryError(f"The API reported a query error: {row['error']}")
        rows.append(row)
    return rows


def unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def to_float(value: object) -> float | None:
    """A cell or value to float; None for blanks, words and NaN."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(unquote(value))
        except ValueError:
            return None
    else:
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f
