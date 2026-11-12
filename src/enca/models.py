"""Sites and the appliances in them.

A site holds one or more heat pumps and, depending on the house, a tank,
thermostats and a controller. Each is its own device publishing its own
readings: the tank temperature, for instance, comes from the TANK, which the
heat pump names in its ``assign/tank`` setting.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, overload

from . import _flux
from ._times import TimeLike, now_utc, parse_duration, parse_time, resolve_range
from .errors import EncaError, NotFound, QueryError
from .readings import HEAT_PUMP_SOURCES, REGISTRY, Reading, Source, mode_label, reading
from .series import Frame, Point, Series

if TYPE_CHECKING:
    from .client import Client
    from .energy import EnergyReport, Period

Kind = Literal["heat_pump", "tank", "thermostat", "controller", "unknown"]

#: How long a site's live values are reused before querying again.
LIVE_TTL = 15.0
#: How far back "now" looks for each reading's last value.
DEFAULT_MAX_AGE = "24h"
#: How far back device settings (``assign/``, ``info/``) are looked up.
CONFIG_LOOKBACK = timedelta(days=7)


def kind_of(device_id: str, subtype: str | None = None) -> Kind:
    """A device's kind from its reported subtype, else its id prefix.

    Subtypes are ``heat-pump``, ``tank``, ``thermostat``, ``controller``. The
    prefixes (``hp_``, ``tk_``, ``tst_``, ``controller``) are a fallback.
    """
    s = (subtype or "").strip().strip('"').lower().replace("_", "-")
    if s in ("heat-pump", "heatpump", "hp"):
        return "heat_pump"
    if s in ("tank", "thermostat", "controller"):
        return s  # type: ignore[return-value]
    d = device_id.lower()
    if d.startswith("hp_"):
        return "heat_pump"
    if d.startswith(("tk_", "tank")):
        return "tank"
    if d.startswith(("tst_", "thermostat")):
        return "thermostat"
    if d.startswith(("controller", "ctl_")):
        return "controller"
    return "unknown"


@dataclass(frozen=True)
class LiveValue:
    value: str
    time: datetime | None


def _code(v: object) -> str | None:
    """``"2"``, ``2`` and ``2.0`` all read as ``"2"``; None when unset."""
    if v is None:
        return None
    f = _flux.to_float(v)
    if f is not None:
        return str(round(f))
    s = _flux.unquote(str(v))
    return s or None


def _truthy(v: object) -> bool | None:
    if v is None:
        return None
    s = _flux.unquote(str(v)).lower()
    if s in ("true", "on"):
        return True
    if s in ("false", "off"):
        return False
    f = _flux.to_float(s)
    return None if f is None else f != 0


# ---- Site ---------------------------------------------------------------------


class Site:
    """One home. ``site.heat_pump`` is the shortcut when there is one."""

    def __init__(
        self,
        client: Client,
        site_id: str,
        *,
        name: str | None = None,
        devices: Sequence[str] | None = None,
        subtypes: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self.id = _flux.safe_id(site_id)
        self.name = name or site_id
        self._device_ids = list(devices) if devices is not None else None
        self._subtypes = dict(subtypes or {})
        self._devices: list[Device] | None = None
        self._config: dict[str, dict[str, str]] | None = None
        self._live: tuple[float, str, dict[str, dict[str, LiveValue]]] | None = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"Site({self.id!r})"

    # -- roster

    def _load_roster(self) -> None:
        if self._device_ids is None:
            try:
                body = self._client._api.get_json(f"/sites/{self.id}/devices")
            except NotFound:
                body = {}
            raw = body.get("devices", []) if isinstance(body, dict) else []
            self._device_ids = [d for d in raw if isinstance(d, str) and d]
            subs = body.get("subtypes") if isinstance(body, dict) else None
            if isinstance(subs, dict):
                self._subtypes.update(
                    {k: v for k, v in subs.items() if isinstance(v, str) and v.strip()}
                )

    @property
    def devices(self) -> list[Device]:
        """Every device on the site, typed (HeatPump, Tank, Thermostat, ...)."""
        with self._lock:
            if self._devices is None:
                self._load_roster()
                ids = sorted(self._device_ids or [])
                kinds = {d: kind_of(d, self._subtypes.get(d)) for d in ids}
                if any(k == "unknown" for k in kinds.values()):
                    cfg = self.config()
                    for d in ids:
                        if kinds[d] == "unknown":
                            kinds[d] = kind_of(d, cfg.get(d, {}).get("info/subtype"))
                cls: dict[Kind, type[Device]] = {
                    "heat_pump": HeatPump,
                    "tank": Tank,
                    "thermostat": Thermostat,
                    "controller": Controller,
                    "unknown": Device,
                }
                self._devices = [cls[kinds[d]](self, d, kinds[d]) for d in ids]
            return list(self._devices)

    def device(self, device_id: str) -> Device:
        for d in self.devices:
            if d.id == device_id:
                return d
        raise NotFound(
            f"No device {device_id!r} on site {self.id!r}. "
            f"Devices: {', '.join(d.id for d in self.devices) or 'none'}"
        )

    @property
    def heat_pumps(self) -> list[HeatPump]:
        return [d for d in self.devices if isinstance(d, HeatPump)]

    @property
    def heat_pump(self) -> HeatPump:
        """The site's heat pump; an error when it has none or several."""
        hps = self.heat_pumps
        if len(hps) == 1:
            return hps[0]
        if not hps:
            raise NotFound(f"Site {self.id!r} has no heat pump.")
        raise EncaError(
            f"Site {self.id!r} has {len(hps)} heat pumps ({', '.join(h.id for h in hps)}); "
            "use site.heat_pumps."
        )

    @property
    def tanks(self) -> list[Tank]:
        return [d for d in self.devices if isinstance(d, Tank)]

    @property
    def tank(self) -> Tank | None:
        """The site's tank (the first heat pump's linked one), or None."""
        hps = self.heat_pumps
        if hps and hps[0].tank is not None:
            return hps[0].tank
        t = self.tanks
        return t[0] if t else None

    @property
    def thermostats(self) -> list[Thermostat]:
        return [d for d in self.devices if isinstance(d, Thermostat)]

    @property
    def controller(self) -> Controller | None:
        c = [d for d in self.devices if isinstance(d, Controller)]
        return c[0] if c else None

    # -- data

    def config(self, refresh: bool = False) -> dict[str, dict[str, str]]:
        """Device settings: device id → ``assign/...``/``info/...`` → value."""
        with self._lock:
            if self._config is None or refresh:
                self._load_roster()
                ids = self._device_ids or []
                out: dict[str, dict[str, str]] = {d: {} for d in ids}
                if ids:
                    stop = now_utc()
                    flux = _flux.latest_flux(
                        site=self.id,
                        devices=ids,
                        start=stop - CONFIG_LOOKBACK,
                        stop=stop,
                        config=True,
                    )
                    for r in self._client._api.query(flux):
                        app, ent = r.get("appliance"), r.get("entity")
                        if app and ent:
                            out.setdefault(app, {})[ent] = _flux.unquote(r.get("_value", ""))
                self._config = out
            return self._config

    def live(
        self, max_age: str | timedelta = DEFAULT_MAX_AGE, refresh: bool = False
    ) -> dict[str, dict[str, LiveValue]]:
        """Last value of every live reading of every device (one query, reused
        for a few seconds): device id → entity → :class:`LiveValue`."""
        key = str(parse_duration(max_age))
        with self._lock:
            if (
                not refresh
                and self._live is not None
                and self._live[1] == key
                and time.monotonic() - self._live[0] < LIVE_TTL
            ):
                return self._live[2]
            ids = [d.id for d in self.devices]
            out: dict[str, dict[str, LiveValue]] = {d: {} for d in ids}
            if ids:
                stop = now_utc()
                flux = _flux.latest_flux(
                    site=self.id, devices=ids, start=stop - parse_duration(max_age), stop=stop
                )
                for r in self._client._api.query(flux):
                    app, ent = r.get("appliance"), r.get("entity")
                    if app and ent:
                        out.setdefault(app, {})[ent] = LiveValue(
                            r.get("_value", ""), parse_time(r.get("_time", ""))
                        )
            self._live = (time.monotonic(), key, out)
            return out

    def refresh(self) -> None:
        """Forget cached roster, settings and live values."""
        with self._lock:
            self._device_ids = None
            self._devices = None
            self._config = None
            self._live = None

    def energy(
        self,
        period: Period | None = None,
        *,
        at: date | None = None,
        start: TimeLike | None = None,
        end: TimeLike | None = None,
        every: str | None = None,
        price: float | None = None,
    ) -> EnergyReport:
        """Energy of all the site's heat pumps together; see :meth:`HeatPump.energy`."""
        from .energy import combine_reports

        hps = self.heat_pumps
        if not hps:
            raise NotFound(f"Site {self.id!r} has no heat pump.")
        reports = [
            h.energy(period, at=at, start=start, end=end, every=every, price=price) for h in hps
        ]
        return combine_reports(reports, price)


# ---- Devices --------------------------------------------------------------------


class Device:
    """Any device on a site."""

    _sources: frozenset[Source] = frozenset()

    def __init__(self, site: Site, device_id: str, kind: Kind) -> None:
        self.site = site
        self.id = device_id
        self.kind = kind

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.id!r})"

    @property
    def config(self) -> dict[str, str]:
        """This device's settings (``assign/...``, ``info/...``)."""
        return self.site.config().get(self.id, {})

    def _live(self, max_age: str | timedelta = DEFAULT_MAX_AGE) -> dict[str, LiveValue]:
        return self.site.live(max_age).get(self.id, {})

    def _device_for(self, r: Reading) -> str:
        return self.id

    @overload
    def history(
        self,
        names: str,
        *,
        start: TimeLike | None = ...,
        end: TimeLike | None = ...,
        last: str | timedelta | None = ...,
        every: str | timedelta | None = ...,
    ) -> Series: ...

    @overload
    def history(
        self,
        names: list[str] | tuple[str, ...],
        *,
        start: TimeLike | None = ...,
        end: TimeLike | None = ...,
        last: str | timedelta | None = ...,
        every: str | timedelta | None = ...,
    ) -> Frame: ...

    def history(
        self,
        names: str | Sequence[str],
        *,
        start: TimeLike | None = None,
        end: TimeLike | None = None,
        last: str | timedelta | None = None,
        every: str | timedelta | None = None,
    ) -> Series | Frame:
        """Bucket means of one reading (a :class:`Series`) or several (a
        :class:`Frame`) over ``last="24h"`` (the default) or ``start``/``end``.

        ``every`` is the bucket size ("1m", "10m", "1h", "1d"); by default
        about 200 buckets. The right storage tier is picked for you.
        """
        single = isinstance(names, str)
        wanted = [names] if isinstance(names, str) else list(names)
        if not wanted:
            raise ValueError("Name at least one reading.")
        readings = [reading(n, self._sources) for n in wanted]
        frame = fetch_history(self, readings, start=start, end=end, last=last, every=every)
        return frame[readings[0].name] if single else frame


def fetch_history(
    dev: Device,
    readings: list[Reading],
    *,
    start: TimeLike | None,
    end: TimeLike | None,
    last: str | timedelta | None,
    every: str | timedelta | None,
    now: datetime | None = None,
) -> Frame:
    now = now or now_utc()
    begin, stop = resolve_range(start, end, last, now=now)
    step = parse_duration(every) if every is not None else _flux.step_for(stop - begin)
    if (stop - begin) / step > _flux.MAX_POINTS:
        raise QueryError(
            f"That would be over {_flux.MAX_POINTS} points per reading; use a larger every=."
        )
    tier = _flux.tier_for_step(begin, stop, now, step)

    stored: dict[str, Reading] = {}
    for r in readings:
        for x in [REGISTRY[i] for i in r.inputs] if r.derived else [r]:
            stored[x.name] = x
    by_device: dict[str, list[Reading]] = {}
    for x in stored.values():
        by_device.setdefault(dev._device_for(x), []).append(x)

    def run(item: tuple[str, list[Reading]]) -> list[dict[str, str]]:
        device, rs = item
        flux = _flux.series_flux(
            tier=tier,
            site=dev.site.id,
            device=device,
            entities=sorted({r.entity for r in rs}),
            start=begin,
            stop=stop,
            every=step,
        )
        return dev.site._client._api.query(flux)

    groups = list(by_device.items())
    results = dev.site._client._map(run, groups)
    raw: dict[str, list[Point]] = {}
    for (_device, rs), rows in zip(groups, results, strict=False):
        by_entity: dict[str, list[Point]] = {}
        for row in rows:
            t, v = parse_time(row.get("_time", "")), _flux.to_float(row.get("_value"))
            ent = row.get("entity")
            if t is None or v is None or not ent:
                continue
            by_entity.setdefault(ent, []).append(Point(t, v))
        for r in rs:
            pts = sorted(by_entity.get(r.entity, []))
            raw[r.name] = [Point(p.time, r.convert(p.value)) for p in pts]

    out: dict[str, Series] = {}
    for r in readings:
        pts = derive(r, raw) if r.derived else raw.get(r.name, [])
        out[r.name] = Series(r.name, r.label, r.unit, pts)
    return Frame(out)


def derive(r: Reading, inputs: dict[str, list[Point]]) -> list[Point]:
    """A derived reading per bucket, only where every input has a value."""
    assert r.combine is not None
    maps = [{p.time: p.value for p in inputs.get(i, [])} for i in r.inputs]
    times = sorted(set(maps[0]).intersection(*maps[1:])) if maps else []
    return [Point(t, r.combine([m[t] for m in maps])) for t in times]


# ---- Heat pump ----------------------------------------------------------------------


@dataclass(frozen=True)
class HeatPumpSnapshot:
    """The heat pump's latest readings, in the units of the reading table.

    Any reading that hasn't reported within the look-back is None. Tank
    readings come from the linked tank device.
    """

    time: datetime | None
    mode: str | None
    mode_raw: str | None
    current_action: str | None
    compressor_running: bool | None
    tank_name: str | None
    cop: float | None = None
    power: float | None = None
    heat_delivered: float | None = None
    heat_from_ground: float | None = None
    ground_loop_in: float | None = None
    ground_loop_out: float | None = None
    ground_loop_flow: float | None = None
    ground_loop_pump: float | None = None
    air_intake: float | None = None
    air_supply: float | None = None
    fan: float | None = None
    tank: float | None = None
    tank_target: float | None = None
    outdoor_air: float | None = None
    water_from_tank: float | None = None
    water_to_tank: float | None = None
    tank_loop_flow: float | None = None
    tank_loop_pump: float | None = None
    heat_into_water: float | None = None
    low_side_pressure: float | None = None
    high_side_pressure: float | None = None
    suction_saturation: float | None = None
    condensing_saturation: float | None = None
    discharge_line: float | None = None
    compressor_inlet: float | None = None
    superheat: float | None = None
    subcooled_liquid: float | None = None
    subcooling: float | None = None
    compressor_speed: float | None = None
    compressor_load: float | None = None

    @property
    def power_kw(self) -> float | None:
        return self.power

    @property
    def compressor_rpm(self) -> float | None:
        return self.compressor_speed

    @property
    def ground_flow(self) -> float | None:
        return self.ground_loop_flow

    def __getitem__(self, name: str) -> Any:
        return getattr(self, reading(name, HEAT_PUMP_SOURCES).name)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def readings(self) -> list[tuple[Reading, float | None]]:
        """(reading, value) for every reading, in table order."""
        names = {f.name for f in fields(self)}
        return [(r, getattr(self, r.name)) for r in REGISTRY.values() if r.name in names]

    def __str__(self) -> str:
        lines = [f"Mode: {self.mode or '—'}"]
        for r, v in self.readings():
            if v is not None:
                label = self.tank_name if r.name == "tank" and self.tank_name else r.label
                lines.append(f"{label}: {r.format(v)}")
        return "\n".join(lines)


SNAPSHOT_READINGS = tuple(r.name for r in REGISTRY.values() if r.source in HEAT_PUMP_SOURCES)


class HeatPump(Device):
    _sources = HEAT_PUMP_SOURCES

    @property
    def tank(self) -> Tank | None:
        """The tank this heat pump heats (its ``assign/tank``), or None.

        If the setting hasn't been seen recently, a site with exactly one
        tank uses that one.
        """
        tid = self.config.get("assign/tank", "").strip()
        tanks = self.site.tanks
        if tid:
            for t in tanks:
                if t.id == tid:
                    return t
            return Tank(self.site, tid, "tank")
        dedicated = _code(
            (self.site.controller.config if self.site.controller else {}).get("assign/dedicated_hw")
        )
        if dedicated == "2":
            return None
        return tanks[0] if len(tanks) == 1 else None

    def _device_for(self, r: Reading) -> str:
        if r.source == "tank":
            t = self.tank
            if t is None:
                raise NotFound(f"Heat pump {self.id!r} has no linked tank, so no {r.name!r}.")
            return t.id
        return self.id

    def now(self, max_age: str | timedelta = DEFAULT_MAX_AGE) -> HeatPumpSnapshot:
        """Latest value of every reading (one query for the whole site)."""
        live = self.site.live(max_age)
        mine = live.get(self.id, {})
        tank = self.tank
        theirs = live.get(tank.id, {}) if tank else {}
        return build_snapshot(mine, theirs, tank_name=tank.name if tank else None)

    @property
    def mode(self) -> str | None:
        return self.now().mode

    @property
    def cop(self) -> float | None:
        return self.now().cop

    def energy(
        self,
        period: Period | None = None,
        *,
        at: date | None = None,
        start: TimeLike | None = None,
        end: TimeLike | None = None,
        every: str | None = None,
        price: float | None = None,
    ) -> EnergyReport:
        """Electricity used, heating/cooling delivered, COP and cost.

        Either a calendar ``period`` ("day", "week", "month", "year") around
        ``at`` (default today, local time), or ``start``/``end`` with
        ``every`` ("1h", "1d", "1mo"). ``price`` is per kWh of electricity.
        """
        from .energy import energy_report

        return energy_report(
            self, period=period, at=at, start=start, end=end, every=every, price=price
        )


def build_snapshot(
    hp: dict[str, LiveValue], tank: dict[str, LiveValue], *, tank_name: str | None = None
) -> HeatPumpSnapshot:
    """A :class:`HeatPumpSnapshot` from the heat pump's and its tank's last values."""

    def raw(r: Reading) -> float | None:
        src = tank if r.source == "tank" else hp
        lv = src.get(r.entity)
        f = _flux.to_float(lv.value) if lv else None
        return None if f is None else r.convert(f)

    speed = raw(REGISTRY["compressor_speed"])
    running = None if speed is None else speed > 0
    values: dict[str, float | None] = {}
    for name in SNAPSHOT_READINGS:
        r = REGISTRY[name]
        if r.derived:
            continue
        v = raw(r)
        if r.while_running and running is False:
            v = None
        values[name] = v
    for name in SNAPSHOT_READINGS:
        r = REGISTRY[name]
        if r.derived and r.combine is not None:
            ins = [values.get(i) for i in r.inputs]
            values[name] = (
                r.combine([x for x in ins if x is not None])
                if all(x is not None for x in ins)
                else None
            )
    times = [lv.time for lv in [*hp.values(), *tank.values()] if lv.time is not None]
    mode_raw = _flux.unquote(hp["data/mode"].value) if "data/mode" in hp else None
    action = _flux.unquote(hp["data/current_action"].value) if "data/current_action" in hp else None
    return HeatPumpSnapshot(
        time=max(times) if times else None,
        mode=mode_label(mode_raw),
        mode_raw=mode_raw,
        current_action=action or None,
        compressor_running=running,
        tank_name=tank_name,
        **values,
    )


# ---- Tank -------------------------------------------------------------------------

TankRole = Literal["hot_water", "heating_water", "heating_water_preheat"]


@dataclass(frozen=True)
class TankSnapshot:
    name: str
    role: TankRole | None
    time: datetime | None
    temp: float | None
    target: float | None
    outdoor_air: float | None
    calling: bool | None
    backup_heat_on: bool | None


class Tank(Device):
    _sources: frozenset[Source] = frozenset({"tank"})

    @property
    def role(self) -> TankRole | None:
        """What the water is for, from the controller's ``dedicated_hw``:
        ``hot_water`` (0), ``heating_water_preheat`` (1), ``heating_water`` (3);
        None when not said."""
        c = self.site.controller
        code = _code(c.config.get("assign/dedicated_hw")) if c else None
        return {"0": "hot_water", "1": "heating_water_preheat", "3": "heating_water"}.get(
            code or ""
        )  # type: ignore[return-value]

    @property
    def name(self) -> str:
        """ "Hot water tank", "Heating water tank", or "Water tank" when unknown."""
        return {
            "hot_water": "Hot water tank",
            "heating_water": "Heating water tank",
            "heating_water_preheat": "Heating water tank",
        }.get(self.role or "", "Water tank")

    def now(self, max_age: str | timedelta = DEFAULT_MAX_AGE) -> TankSnapshot:
        d = self._live(max_age)

        def f(e: str) -> float | None:
            return _flux.to_float(d[e].value) if e in d else None

        times = [lv.time for lv in d.values() if lv.time is not None]
        return TankSnapshot(
            name=self.name,
            role=self.role,
            time=max(times) if times else None,
            temp=f("data/t1"),
            target=f("data/setpoint"),
            outdoor_air=f("data/t15"),
            calling=_truthy(d["data/heating_relay"].value) if "data/heating_relay" in d else None,
            backup_heat_on=_truthy(d["data/backup_heat_ctrl"].value)
            if "data/backup_heat_ctrl" in d
            else None,
        )

    @property
    def temp(self) -> float | None:
        return self.now().temp

    @property
    def target(self) -> float | None:
        return self.now().target


# ---- Thermostat ---------------------------------------------------------------------

Call = Literal["heat", "cool", "satisfied", "off"]


@dataclass(frozen=True)
class ThermostatSnapshot:
    name: str
    time: datetime | None
    temp: float | None
    target: float | None
    humidity: float | None
    mode: str | None
    call: Call | None

    @property
    def calling(self) -> bool:
        """Calling for heat or cooling right now."""
        return self.call in ("heat", "cool")


def thermostat_mode(mode: str | None) -> str | None:
    return {
        "off": "Off",
        "heating": "Heating",
        "heat": "Heating",
        "cooling": "Cooling",
        "cool": "Cooling",
        "emergency": "Emergency heat",
    }.get((mode or "").lower())


def thermostat_call(
    d: dict[str, str], mode: str | None, temp: float | None, target: float | None
) -> Call | None:
    """Same rules as the app's thermostat screen."""
    if mode == "off":
        return "off"
    heat, cool = (
        _flux.to_float(d.get("data/heating_demand")),
        _flux.to_float(d.get("data/cooling_demand")),
    )
    if heat is not None or cool is not None:
        if (heat or 0) > 0:
            return "heat"
        if (cool or 0) > 0:
            return "cool"
        return "satisfied"
    if _truthy(d.get("data/heating_relay")):
        return "heat"
    if _truthy(d.get("data/cooling_relay")):
        return "cool"
    if temp is None or target is None or mode is None:
        return None
    if ("heat" in mode or mode == "emergency") and temp < target:
        return "heat"
    if "cool" in mode and temp > target:
        return "cool"
    return "satisfied"


class Thermostat(Device):
    _sources: frozenset[Source] = frozenset({"thermostat"})

    @property
    def name(self) -> str:
        """Its own name (``assign/name``), else "Zone N" in device-id order."""
        n = self.config.get("assign/name", "").strip()
        if n:
            return n
        ids = sorted(t.id for t in self.site.thermostats)
        return f"Zone {ids.index(self.id) + 1}" if self.id in ids else "Zone"

    @property
    def zone_type(self) -> str | None:
        """``fa`` (forced air), ``hw`` (hot water), ``hybrid`` or ``disabled``."""
        return self.config.get("assign/type", "").strip().lower() or None

    @property
    def heated_by(self) -> str | None:
        return {"fa": "Forced air", "hw": "Hot water", "hybrid": "Forced air and hot water"}.get(
            self.zone_type or ""
        )

    @property
    def used(self) -> bool:
        return (
            self.zone_type != "disabled" and _truthy(self.config.get("assign/enabled")) is not False
        )

    def now(self, max_age: str | timedelta = DEFAULT_MAX_AGE) -> ThermostatSnapshot:
        live = self._live(max_age)
        d = {k: v.value for k, v in live.items()}
        temp = _flux.to_float(d.get("data/temp"))
        target = _flux.to_float(d.get("data/setpoint"))
        if target is None:
            target = _flux.to_float(self.config.get("assign/setpoint"))
        mode = _flux.unquote(d["data/mode"]).lower() if "data/mode" in d else None
        times = [lv.time for lv in live.values() if lv.time is not None]
        return ThermostatSnapshot(
            name=self.name,
            time=max(times) if times else None,
            temp=temp,
            target=target,
            humidity=_flux.to_float(d.get("data/humidity")),
            mode=thermostat_mode(mode),
            call=thermostat_call(d, mode, temp, target),
        )

    @property
    def temp(self) -> float | None:
        return self.now().temp

    @property
    def target(self) -> float | None:
        return self.now().target

    @property
    def humidity(self) -> float | None:
        return self.now().humidity

    @property
    def mode(self) -> str | None:
        return self.now().mode

    @property
    def calling(self) -> bool:
        return self.now().calling


# ---- Controller ---------------------------------------------------------------------


class Controller(Device):
    """The site controller: how the water side is plumbed."""

    @property
    def dedicated_hw(self) -> str | None:
        """``0`` dedicated hot water heater, ``1`` hydronic heating with hot
        water preheat, ``2`` no tank, ``3`` hydronic heating without preheat."""
        return _code(self.config.get("assign/dedicated_hw"))

    @property
    def makes_hot_water(self) -> bool | None:
        return _truthy(self.config.get("assign/dhw"))

    @property
    def air_model(self) -> str | None:
        """``fa`` forced air only, ``hybrid`` forced air plus hot water."""
        return self.config.get("assign/air_model") or None
