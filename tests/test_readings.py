import pytest

import enca
from enca.models import LiveValue, build_snapshot
from enca.readings import mode_label, reading, subcooling


def test_unknown_name_lists_valid_names_and_suggests() -> None:
    with pytest.raises(enca.NotFound) as e:
        reading("ground_loop_inn")
    msg = str(e.value)
    assert "Did you mean 'ground_loop_in'" in msg
    assert "cop" in msg and "low_side_pressure" in msg


def test_names_are_forgiving() -> None:
    assert reading("Ground Loop In").entity == "data/t9"
    assert reading("low-side-pressure").entity == "data/p1"


def test_thermostat_names_not_valid_for_heat_pump() -> None:
    with pytest.raises(enca.NotFound):
        reading("humidity", enca.readings.HEAT_PUMP_SOURCES)


def test_unit_scaling() -> None:
    assert reading("power").convert(2400) == pytest.approx(2.4)  # W → kW
    assert reading("heat_into_water").convert(3412.14) == pytest.approx(1.0)  # BTU/h → kW
    assert reading("power").format(2.4) == "2.40 kW"
    assert reading("cop").format(3.8) == "3.80"
    assert reading("cop").format(None) == "—"


def test_tank_reading_lives_on_tank() -> None:
    assert reading("tank").source == "tank"
    assert reading("tank").entity == "data/t1"


def test_subcooling() -> None:
    assert subcooling(105.0, 98.5) == pytest.approx(6.5)
    r = reading("subcooling")
    assert r.derived and r.inputs == ("condensing_saturation", "subcooled_liquid")


def test_mode_labels_match_app() -> None:
    assert mode_label('"forced_air"') == "Forced air"
    assert mode_label("hw") == "Hot water"
    assert mode_label("hybrid") == "Hybrid"
    assert mode_label("cooling_dhw") == "Cooling dhw"
    assert mode_label("") is None


def lv(v: str) -> LiveValue:
    return LiveValue(v, None)


def test_snapshot_derives_and_gates() -> None:
    hp = {
        "data/refr_cond_temp": lv("105"),
        "data/t6": lv("98.5"),
        "data/refr_sh_final": lv("8.4"),
        "bus/inverter/data/compressor_running_speed": lv("0"),
        "data/total_power": lv("1500"),
    }
    s = build_snapshot(hp, {})
    assert s.subcooling == pytest.approx(6.5)
    assert s.superheat is None  # compressor stopped: no leftover superheat
    assert s.compressor_running is False
    assert s.power == pytest.approx(1.5) and s.power_kw == s.power
    assert s.tank is None


def test_snapshot_fields_cover_registry() -> None:
    import dataclasses

    fields = {f.name for f in dataclasses.fields(enca.HeatPumpSnapshot)}
    for name in enca.readings.names(enca.readings.HEAT_PUMP_SOURCES):
        assert name in fields
