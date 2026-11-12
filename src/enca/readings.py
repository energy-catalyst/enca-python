"""The one registry of friendly reading names.

Each reading maps a plain name (snake_case of the label the Energy Catalyst
app shows) to the appliance that publishes it, the stored entity, the unit
and the scale that turns the stored value into that unit. Labels match the
app's "Über Geek" sensor wall word for word.

Use :func:`reading` to look one up and :func:`names` for the valid names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from .errors import NotFound

#: Which appliance publishes a reading. Tank readings live on the TANK
#: appliance the heat pump is linked to (heat pump ``assign/tank``), not on
#: the heat pump.
Source = Literal["heat_pump", "tank", "thermostat"]

BTU_H_PER_KW = 3412.14


@dataclass(frozen=True)
class Reading:
    name: str
    label: str
    entity: str
    unit: str
    source: Source = "heat_pump"
    scale: float = 1.0
    decimals: int = 1
    meaning: str = ""
    #: Only meaningful while the compressor turns (superheat): reported as
    #: None in snapshots when the compressor is known to be stopped.
    while_running: bool = False
    #: Part of the tank side; absent when the site has no tank.
    water: bool = False
    #: For readings worked out from others (not stored).
    inputs: tuple[str, ...] = ()
    combine: Callable[[list[float]], float] | None = field(default=None, compare=False)

    @property
    def derived(self) -> bool:
        return bool(self.inputs)

    def convert(self, raw: float) -> float:
        """Stored value to :attr:`unit`."""
        return raw * self.scale

    def format(self, value: float | None) -> str:
        """``"45.2 °F"``, ``"2.40 kW"``, ``"3.80"``; ``"—"`` for None."""
        if value is None:
            return "—"
        n = f"{value:.{self.decimals}f}"
        return f"{n} {self.unit}" if self.unit else n


def subcooling(condensing_saturation: float, subcooled_liquid: float) -> float:
    """Subcooling, °F: how far the liquid leaving the condenser sits below
    the condensing saturation temperature. Not published; derived."""
    return condensing_saturation - subcooled_liquid


def _subcool(v: list[float]) -> float:
    return subcooling(v[0], v[1])


_ALL: tuple[Reading, ...] = (
    # Power
    Reading(
        "cop",
        "COP",
        "data/cop",
        "",
        decimals=2,
        meaning="Coefficient of performance: heat moved per unit of electricity",
    ),
    Reading(
        "power",
        "Power in",
        "data/total_power",
        "kW",
        scale=0.001,
        decimals=2,
        meaning="Electrical power the heat pump draws (stored in W)",
    ),
    Reading(
        "heat_delivered",
        "Heat delivered",
        "data/heat_rejection",
        "kW",
        scale=1 / BTU_H_PER_KW,
        decimals=2,
        meaning="All heat delivered to the house (stored in BTU/h)",
    ),
    Reading(
        "heat_from_ground",
        "Heat from ground",
        "data/heat_extraction",
        "kW",
        scale=1 / BTU_H_PER_KW,
        decimals=2,
        meaning="Heat drawn from the ground loop (stored in BTU/h)",
    ),
    # Ground loop
    Reading(
        "ground_loop_in",
        "Ground loop, from ground",
        "data/t9",
        "°F",
        meaning="Ground loop fluid arriving from the ground",
    ),
    Reading(
        "ground_loop_out",
        "Ground loop, back to ground",
        "data/t10",
        "°F",
        meaning="Ground loop fluid returning to the ground",
    ),
    Reading(
        "ground_loop_flow", "Ground loop flow", "data/q1", "GPM", meaning="Ground loop flow rate"
    ),
    Reading(
        "ground_loop_pump",
        "Ground loop pump",
        "data/gl",
        "%",
        decimals=0,
        meaning="Ground loop pump speed",
    ),
    # Air
    Reading(
        "air_intake", "Air intake", "data/t13", "°F", meaning="Return air entering the air handler"
    ),
    Reading(
        "air_supply", "Air supply", "data/t14", "°F", meaning="Supply air leaving the air handler"
    ),
    Reading("fan", "Fan speed", "data/fan", "%", decimals=0, meaning="Blower speed"),
    # Tank (on the TANK appliance)
    Reading(
        "tank",
        "Water tank",
        "data/t1",
        "°F",
        source="tank",
        water=True,
        meaning="Tank temperature (published by the tank, not the heat pump)",
    ),
    Reading(
        "tank_target",
        "Tank target",
        "data/setpoint",
        "°F",
        source="tank",
        water=True,
        meaning="Tank target temperature",
    ),
    Reading(
        "outdoor_air",
        "Outdoor air",
        "data/t15",
        "°F",
        source="tank",
        water=True,
        meaning="Outdoor air, where the tank controller has a sensor wired",
    ),
    # Tank loop (on the heat pump)
    Reading(
        "water_from_tank",
        "Water back from tank",
        "data/t11",
        "°F",
        water=True,
        meaning="Water coming back from the tank into the heat pump",
    ),
    Reading(
        "water_to_tank",
        "Water to tank",
        "data/t12",
        "°F",
        water=True,
        meaning="Water leaving the heat pump for the tank",
    ),
    Reading(
        "tank_loop_flow",
        "Tank loop flow",
        "data/q2",
        "GPM",
        water=True,
        meaning="Flow between heat pump and tank",
    ),
    Reading(
        "tank_loop_pump",
        "Tank loop pump",
        "data/rc",
        "%",
        decimals=0,
        water=True,
        meaning="The heat pump's tank loop pump speed",
    ),
    Reading(
        "heat_into_water",
        "Heat into water",
        "data/hw_heat_rejection",
        "kW",
        scale=1 / BTU_H_PER_KW,
        decimals=2,
        water=True,
        meaning="Heat going into the tank water (stored in BTU/h)",
    ),
    # Refrigerant
    Reading(
        "low_side_pressure",
        "Low-side pressure",
        "data/p1",
        "psi",
        meaning="Suction pressure (gauge)",
    ),
    Reading(
        "high_side_pressure",
        "High-side pressure",
        "data/p2",
        "psi",
        meaning="Discharge pressure (gauge)",
    ),
    Reading(
        "suction_saturation",
        "Suction saturation",
        "data/refr_evap_temp",
        "°F",
        meaning="Saturation temperature at suction pressure",
    ),
    Reading(
        "condensing_saturation",
        "Condensing saturation",
        "data/refr_cond_temp",
        "°F",
        meaning="Saturation temperature at discharge pressure",
    ),
    Reading(
        "discharge_line",
        "Discharge line",
        "data/t2",
        "°F",
        meaning="Compressor discharge line temperature",
    ),
    Reading(
        "compressor_inlet",
        "Compressor inlet",
        "data/t8",
        "°F",
        meaning="Refrigerant entering the compressor",
    ),
    Reading(
        "superheat",
        "Superheat",
        "data/refr_sh_final",
        "°F",
        while_running=True,
        meaning="Superheat at the compressor (only while it runs)",
    ),
    Reading(
        "subcooled_liquid",
        "Subcooled liquid",
        "data/t6",
        "°F",
        meaning="Liquid refrigerant leaving the condenser",
    ),
    Reading(
        "subcooling",
        "Subcooling",
        "",
        "°F",
        inputs=("condensing_saturation", "subcooled_liquid"),
        combine=_subcool,
        meaning="Condensing saturation minus subcooled liquid (derived)",
    ),
    # Compressor
    Reading(
        "compressor_speed",
        "Compressor speed",
        "bus/inverter/data/compressor_running_speed",
        "RPM",
        decimals=0,
        meaning="Compressor speed reported by the inverter",
    ),
    Reading(
        "compressor_load",
        "Compressor load",
        "data/comp_load_factor",
        "%",
        decimals=0,
        meaning="Compressor load factor",
    ),
    # Thermostats
    Reading(
        "temp",
        "Room",
        "data/temp",
        "°F",
        source="thermostat",
        meaning="Room temperature at a thermostat",
    ),
    Reading(
        "target",
        "Target",
        "data/setpoint",
        "°F",
        source="thermostat",
        meaning="Thermostat target temperature",
    ),
    Reading(
        "humidity",
        "Humidity",
        "data/humidity",
        "%",
        source="thermostat",
        decimals=0,
        meaning="Room humidity at a thermostat",
    ),
)

REGISTRY: dict[str, Reading] = {r.name: r for r in _ALL}

#: Readings a heat pump's history/snapshot can address (its own and its tank's).
HEAT_PUMP_SOURCES: frozenset[Source] = frozenset({"heat_pump", "tank"})


def names(sources: Iterable[Source] | None = None) -> list[str]:
    """Valid reading names, optionally only those of some ``sources``."""
    allowed = set(sources) if sources is not None else None
    return [r.name for r in _ALL if allowed is None or r.source in allowed]


def reading(name: str, sources: Iterable[Source] | None = None) -> Reading:
    """Look up a reading by friendly name; :class:`NotFound` lists valid ones."""
    allowed = set(sources) if sources is not None else None
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    r = REGISTRY.get(key)
    if r is None or (allowed is not None and r.source not in allowed):
        valid = names(allowed)
        hint = _closest(key, valid)
        raise NotFound(
            f"Unknown reading {name!r}."
            + (f" Did you mean {hint!r}?" if hint else "")
            + " Valid names: "
            + ", ".join(valid)
        )
    return r


def _closest(key: str, valid: list[str]) -> str | None:
    import difflib

    m = difflib.get_close_matches(key, valid, n=1, cutoff=0.6)
    return m[0] if m else None


def mode_label(mode: object) -> str | None:
    """The heat pump's ``data/mode`` in plain words, like the app."""
    if mode is None:
        return None
    m = str(mode).strip().strip('"').lower()
    if not m:
        return None
    return {
        "off": "Off",
        "heating": "Heating",
        "heat": "Heating",
        "cooling": "Cooling",
        "cool": "Cooling",
        "hw": "Hot water",
        "dhw": "Hot water",
        "hot_water": "Hot water",
        "hybrid": "Hybrid",
    }.get(m, m[0].upper() + m[1:].replace("_", " "))
