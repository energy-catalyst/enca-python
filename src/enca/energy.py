"""Energy: electricity used, heat (and cooling) delivered, COP and cost.

Follows the app's "Energy, cost & COP" screen:

1. **Counters (exact).** Heat pumps keep running energy counters
   (``energy/electric_wh``, ``energy/heating_wh``, ...). A bucket's energy is
   each counter's rise across it, read from their last value per hour.
   A bucket is covered when a counter value exists before it begins.
2. **Estimate (older data / no counters).** Electricity is the sum of
   total power's 1-minute means. Heat is estimated per hour as electricity ×
   the hour's mean COP while the unit heats (by its mode).

   TODO: the app also estimates cooling delivered and splits the
   ``cooling_dhw`` mode between cooling and tank heating; here estimated
   buckets with cooling hours report ``cooling_kwh=None`` and count
   ``cooling_dhw`` as cooling.

Buckets follow local calendar time (DST-aware), hours are read per hour and
grouped client-side, and queries are chunked to at most 31 days each.
"""

from __future__ import annotations

import calendar
import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast

from . import _flux
from ._times import TimeLike, now_utc, parse_time, to_utc

if TYPE_CHECKING:
    import pandas as pd

    from .models import HeatPump

Period = Literal["day", "week", "month", "year"]

HEATING_MODES = frozenset(
    {"manual", "hybrid", "forced_air", "hw", "backup_heat", "heating", "heat"}
)
COOLING_MODES = frozenset({"cooling", "cooling_dhw", "cool"})

CHUNK = timedelta(days=31)


@dataclass
class EnergyBucket:
    """One bucket of an :class:`EnergyReport`; energies in kWh."""

    start: datetime  # local time, aware
    end: datetime
    electricity_kwh: float = 0.0
    heating_kwh: float | None = 0.0
    cooling_kwh: float | None = 0.0
    heating_electricity_kwh: float = 0.0
    cooling_electricity_kwh: float = 0.0
    ground_kwh: float | None = 0.0
    #: Bucket's mean COP reported by the unit (used when there are no counters).
    mean_cop: float | None = None
    cost: float | None = None
    #: From the energy counters (True) or estimated (False).
    exact: bool = True

    @property
    def cop(self) -> float | None:
        """Heating delivered ÷ electricity used for heating, else the mean COP."""
        if self.heating_kwh is not None and self.heating_electricity_kwh > 0.05:
            return self.heating_kwh / self.heating_electricity_kwh
        return self.mean_cop


@dataclass
class EnergyReport:
    buckets: list[EnergyBucket]
    price: float | None = None
    devices: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[EnergyBucket]:
        return iter(self.buckets)

    def __len__(self) -> int:
        return len(self.buckets)

    @property
    def electricity_kwh(self) -> float:
        return sum(b.electricity_kwh for b in self.buckets)

    @property
    def heating_kwh(self) -> float | None:
        vals = [b.heating_kwh for b in self.buckets]
        return None if any(v is None for v in vals) else sum(v for v in vals if v is not None)

    @property
    def cooling_kwh(self) -> float | None:
        vals = [b.cooling_kwh for b in self.buckets]
        return None if any(v is None for v in vals) else sum(v for v in vals if v is not None)

    @property
    def cop(self) -> float | None:
        heat = self.heating_kwh
        elec = sum(b.heating_electricity_kwh for b in self.buckets)
        if heat is not None and elec > 0.05:
            return heat / elec
        cops = [(b.mean_cop, b.electricity_kwh) for b in self.buckets if b.mean_cop is not None]
        w = sum(e for _, e in cops)
        return sum(c * e for c, e in cops) / w if w > 0 else None

    @property
    def cost(self) -> float | None:
        return None if self.price is None else self.electricity_kwh * self.price

    @property
    def exact(self) -> bool:
        return all(b.exact for b in self.buckets)

    def to_dataframe(self) -> pd.DataFrame:
        from .series import _pandas

        pdm = _pandas()
        rows: list[dict[str, Any]] = [
            {
                "start": b.start,
                "end": b.end,
                "electricity_kwh": b.electricity_kwh,
                "heating_kwh": b.heating_kwh,
                "cooling_kwh": b.cooling_kwh,
                "ground_kwh": b.ground_kwh,
                "cop": b.cop,
                "cost": b.cost,
                "exact": b.exact,
            }
            for b in self.buckets
        ]
        return cast("pd.DataFrame", pdm.DataFrame(rows).set_index("start"))

    def __str__(self) -> str:
        def k(v: float | None) -> str:
            return "—" if v is None else f"{v:.1f} kWh"

        parts = [f"Electricity: {k(self.electricity_kwh)}", f"Heating: {k(self.heating_kwh)}"]
        if self.cooling_kwh:
            parts.append(f"Cooling: {k(self.cooling_kwh)}")
        cop = self.cop
        parts.append(f"COP: {'—' if cop is None else f'{cop:.2f}'}")
        if self.cost is not None:
            parts.append(f"Cost: {self.cost:.2f}")
        return " · ".join(parts)


# ---- Buckets ----------------------------------------------------------------------


def _local(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """A local wall-clock time, aware (the machine's time zone, DST-aware)."""
    return datetime(year, month, day, hour).astimezone()


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_buckets(period: Period, at: date) -> list[tuple[datetime, datetime]]:
    """Local calendar buckets: a day by hour, a week (Mon-Sun) or month by
    day, a year by month."""
    if period == "day":
        # Each local hour of the day; a DST day has 23 or 25 of them.
        begin = _local(at.year, at.month, at.day)
        nxt = at + timedelta(days=1)
        end = _local(nxt.year, nxt.month, nxt.day)
        edges = []
        t = begin.astimezone(timezone.utc)
        while t < end:
            edges.append(t.astimezone())
            t += timedelta(hours=1)
        edges.append(end)
        return list(itertools.pairwise(edges))
    if period == "week":
        first = at - timedelta(days=at.weekday())
        days = [first + timedelta(days=i) for i in range(8)]
    elif period == "month":
        n = calendar.monthrange(at.year, at.month)[1]
        days = [date(at.year, at.month, 1) + timedelta(days=i) for i in range(n + 1)]
    elif period == "year":
        days = [_add_months(date(at.year, 1, 1), i) for i in range(13)]
    else:
        raise ValueError("period must be 'day', 'week', 'month' or 'year'.")
    edges = [_local(d.year, d.month, d.day) for d in days]
    return list(itertools.pairwise(edges))


def range_buckets(start: datetime, end: datetime, every: str) -> list[tuple[datetime, datetime]]:
    """Local buckets of ``every`` ("1h", "1d", "1w", "1mo") covering [start, end)."""
    e = every.strip().lower()
    s_local = start.astimezone()
    edges: list[datetime] = []
    if e in ("1h", "h", "hour"):
        t = s_local.replace(minute=0, second=0, microsecond=0)
        while t < end:
            edges.append(t)
            t = (t.astimezone(timezone.utc) + timedelta(hours=1)).astimezone()
        edges.append(t)
    elif e in ("1d", "d", "day", "1w", "w", "week"):
        step = 7 if e in ("1w", "w", "week") else 1
        d = s_local.date()
        if step == 7:
            d -= timedelta(days=d.weekday())
        while True:
            t = _local(d.year, d.month, d.day)
            edges.append(t)
            if t >= end:
                break
            d += timedelta(days=step)
    elif e in ("1mo", "mo", "month"):
        d = date(s_local.year, s_local.month, 1)
        while True:
            t = _local(d.year, d.month, d.day)
            edges.append(t)
            if t >= end:
                break
            d = _add_months(d, 1)
    else:
        raise ValueError("every must be '1h', '1d', '1w' or '1mo' for energy.")
    return list(itertools.pairwise(edges))


# ---- Hourly inputs ----------------------------------------------------------------------


@dataclass
class Hours:
    """Hourly inputs parsed from :func:`enca._flux.energy_hours_flux` rows."""

    counters: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    power_wmin: dict[datetime, float] = field(default_factory=dict)
    cop: dict[datetime, float] = field(default_factory=dict)
    mode: dict[datetime, str] = field(default_factory=dict)

    def add_rows(self, rows: Sequence[dict[str, str]]) -> None:
        for r in rows:
            t = parse_time(r.get("_time", ""))
            if t is None:
                continue
            q = r.get("result", "")
            if q == "mode":
                m = _flux.unquote(r.get("_value", "")).lower()
                if m:
                    self.mode[t] = m
                continue
            v = _flux.to_float(r.get("_value"))
            if v is None:
                continue
            if q == "counter":
                self.counters.setdefault(r.get("entity", ""), []).append((t, v))
            elif q == "power":
                self.power_wmin[t] = self.power_wmin.get(t, 0.0) + v
            elif q == "cop":
                self.cop[t] = v

    def rise(self, entity: str, s: datetime, e: datetime) -> float | None:
        """The counter's rise over [s, e); None without a value before s."""
        prev: float | None = None
        last: float | None = None
        for t, v in self.counters.get(entity, []):
            if t < s:
                prev = v if prev is None else max(prev, v)
            elif t < e:
                last = v if last is None else max(last, v)
        if prev is None:
            return None
        return max(0.0, (last if last is not None else prev) - prev)


def compute_bucket(h: Hours, s: datetime, e: datetime, price: float | None) -> EnergyBucket:
    s_u, e_u = s.astimezone(timezone.utc), e.astimezone(timezone.utc)
    b = EnergyBucket(start=s, end=e)
    hours = sorted(t for t in h.power_wmin if s_u <= t < e_u)
    cops = [(h.cop[t], h.power_wmin.get(t, 0.0)) for t in h.cop if s_u <= t < e_u]
    w = sum(p for _, p in cops)
    b.mean_cop = (
        sum(c * p for c, p in cops) / w
        if w > 0
        else (sum(c for c, _ in cops) / len(cops) if cops else None)
    )
    elec = h.rise("energy/electric_wh", s_u, e_u)
    if elec is not None:

        def k(ent: str) -> float:
            return (h.rise(ent, s_u, e_u) or 0.0) / 1000

        b.electricity_kwh = elec / 1000
        b.heating_electricity_kwh = k("energy/electric_heating_wh")
        b.cooling_electricity_kwh = k("energy/electric_cooling_wh")
        b.heating_kwh = k("energy/heating_wh")
        b.cooling_kwh = k("energy/cooling_wh")
        b.ground_kwh = k("energy/ground_wh")
    else:
        b.exact = False
        b.ground_kwh = None
        modes = sorted(h.mode.items())
        heat = cool_e = heat_e = 0.0
        any_cool = False
        for t in hours:
            kwh = h.power_wmin[t] / 60000  # Σ minute-mean W × 1 min → kWh
            b.electricity_kwh += kwh
            mode = None
            for mt, m in modes:  # last mode at or before this hour
                if mt <= t:
                    mode = m
                else:
                    break
            if mode in COOLING_MODES:
                any_cool = True
                cool_e += kwh
            elif mode in HEATING_MODES:
                heat_e += kwh
                c = h.cop.get(t)
                if c is not None:
                    heat += kwh * c
        b.heating_kwh = heat
        b.heating_electricity_kwh = heat_e
        b.cooling_electricity_kwh = cool_e
        b.cooling_kwh = None if any_cool else 0.0
    b.cost = None if price is None else b.electricity_kwh * price
    return b


def _chunks(s: datetime, e: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    while s < e:
        n = min(e, s + CHUNK)
        out.append((s, n))
        s = n
    return out


def energy_report(
    hp: HeatPump,
    *,
    period: Period | None,
    at: date | None,
    start: TimeLike | None,
    end: TimeLike | None,
    every: str | None,
    price: float | None,
    now: datetime | None = None,
) -> EnergyReport:
    now = now or now_utc()
    if period is not None:
        if start is not None or end is not None:
            raise ValueError("Pass either period=... or start=/end=, not both.")
        buckets = period_buckets(period, at or now.astimezone().date())
    else:
        if start is None:
            raise ValueError("Pass period='day'|'week'|'month'|'year', or start= (and end=).")
        s, e = to_utc(start), to_utc(end) if end is not None else now
        buckets = range_buckets(s, e, every or "1d")
    # Nothing to read after the end of the current hour.
    horizon = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    live = [(s, min(e, horizon)) for s, e in buckets if s < horizon]
    hours = Hours()
    if live:
        q_start = live[0][0].astimezone(timezone.utc)
        q_end = live[-1][1].astimezone(timezone.utc)
        site = hp.site

        def run(span: tuple[datetime, datetime]) -> list[dict[str, str]]:
            flux = _flux.energy_hours_flux(site=site.id, device=hp.id, start=span[0], stop=span[1])
            return site._client._api.query(flux)

        for rows in site._client._map(run, _chunks(q_start, q_end)):
            hours.add_rows(rows)
        for c in hours.counters.values():
            c.sort()
    out = []
    for s, e in buckets:
        if s >= horizon:
            out.append(EnergyBucket(start=s, end=e, cost=None if price is None else 0.0))
        else:
            b = compute_bucket(hours, s, min(e, horizon), price)
            out.append(replace(b, end=e))
    return EnergyReport(out, price, [hp.id])


def combine_reports(reports: Sequence[EnergyReport], price: float | None) -> EnergyReport:
    """Bucket-wise sum of several heat pumps' reports over the same buckets."""
    if len(reports) == 1:
        return reports[0]
    out = []
    for group in zip(*(r.buckets for r in reports), strict=False):
        first = group[0]

        def total(attr: str, g: tuple[EnergyBucket, ...] = group) -> float | None:
            vals = [getattr(b, attr) for b in g]
            return None if any(v is None for v in vals) else sum(vals)

        elec = sum(b.electricity_kwh for b in group)
        cops = [(b.mean_cop, b.electricity_kwh) for b in group if b.mean_cop is not None]
        w = sum(e for _, e in cops)
        out.append(
            EnergyBucket(
                start=first.start,
                end=first.end,
                electricity_kwh=elec,
                heating_kwh=total("heating_kwh"),
                cooling_kwh=total("cooling_kwh"),
                heating_electricity_kwh=sum(b.heating_electricity_kwh for b in group),
                cooling_electricity_kwh=sum(b.cooling_electricity_kwh for b in group),
                ground_kwh=total("ground_kwh"),
                mean_cop=sum(c * e for c, e in cops) / w if w > 0 else None,
                cost=None if price is None else elec * price,
                exact=all(b.exact for b in group),
            )
        )
    devices: list[str] = []
    for r in reports:
        devices.extend(r.devices)
    return EnergyReport(out, price, devices)
