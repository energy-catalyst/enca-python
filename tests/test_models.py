import re
from datetime import timedelta

import pytest

import enca
from conftest import HP, SITE, TANK, FakeApi


def test_sites_and_typed_devices(client: enca.Client) -> None:
    sites = client.sites()
    assert [s.id for s in sites] == [SITE]
    site = client.site()
    kinds = {d.id: d.kind for d in site.devices}
    assert kinds == {
        HP: "heat_pump",
        TANK: "tank",
        "tst_000001": "thermostat",
        "tst_000002": "thermostat",
        "controller": "controller",
    }
    assert site.heat_pump.id == HP
    assert isinstance(site.tank, enca.Tank) and site.tank.id == TANK
    assert site.controller is not None


def test_unknown_site(client: enca.Client) -> None:
    with pytest.raises(enca.NotFound, match="s_example"):
        client.site("s_nope")


def test_kind_from_config_when_prefix_unknown(fake: FakeApi, client: enca.Client) -> None:
    fake.devices = [HP, "zz_1"]
    fake.subtypes = {}
    fake.config["zz_1"] = {"info/subtype": "tank"}
    site = client.site()
    assert {d.id: d.kind for d in site.devices}["zz_1"] == "tank"


def test_now_reads_tank_from_tank_device(fake: FakeApi, client: enca.Client) -> None:
    snap = client.site().heat_pump.now()
    assert snap.tank == pytest.approx(124.5)  # from tk_000001, not the heat pump's stray t1
    assert snap.tank_target == pytest.approx(125)
    assert snap.tank_name == "Hot water tank"  # controller dedicated_hw 0
    assert snap.cop == pytest.approx(3.8)
    assert snap.power == pytest.approx(2.4)
    assert snap.mode == "Forced air" and snap.mode_raw == "forced_air"
    assert snap.subcooling == pytest.approx(6.5)
    assert snap.superheat == pytest.approx(8.4)
    assert snap.compressor_rpm == 3600
    assert snap.heat_into_water == pytest.approx(2.0)
    assert snap["ground loop in"] == pytest.approx(45.2)
    assert "Hot water tank: 124.5 °F" in str(snap)
    live = [q for q in fake.queries if "last()" in q and "(data|remote)" in q]
    assert len(live) == 1  # one live query for the whole site


def test_tank_and_thermostats(client: enca.Client) -> None:
    site = client.site()
    t = site.tank
    assert t is not None and t.role == "hot_water" and t.temp == pytest.approx(124.5)
    assert t.now().calling is True
    a, b = site.thermostats
    assert a.name == "Living room" and b.name == "Zone 2"
    sa = a.now()
    assert sa.temp == 70.5 and sa.target == 72 and sa.humidity == 41
    assert sa.mode == "Heating" and sa.call == "heat" and sa.calling
    assert b.now().call == "satisfied"
    assert a.heated_by == "Forced air" and b.heated_by == "Hot water"


def test_no_tank_when_controller_says_so(fake: FakeApi, client: enca.Client) -> None:
    fake.config[HP].pop("assign/tank")
    fake.config["controller"]["assign/dedicated_hw"] = "2"
    hp = client.site().heat_pump
    assert hp.tank is None
    with pytest.raises(enca.NotFound, match="no linked tank"):
        hp.history("tank")


def _flat(value: float):
    def hist(app, entity, start, stop, every):
        n = min(int((stop - start) / every), 5)
        base = {
            "data/t9": 45.0,
            "data/cop": 3.5,
            "data/t1": 120.0,
            "data/refr_cond_temp": 100.0,
            "data/t6": 95.0,
            "data/total_power": 2000.0,
        }.get(entity, value)
        return [base + i for i in range(n)]

    return hist


def test_history_series_and_frame(fake: FakeApi, client: enca.Client) -> None:
    fake.history = _flat(1.0)
    hp = client.site().heat_pump
    s = hp.history("cop", last="24h")
    assert isinstance(s, enca.Series) and s.unit == "" and s.label == "COP"
    assert s.values == [3.5, 4.5, 5.5, 6.5, 7.5]
    assert s.mean() == pytest.approx(5.5)
    q = fake.queries[-1]
    assert q.startswith("telemetry_1m(") and "every: 15m" in q

    fake.queries.clear()
    f = hp.history(["ground_loop_in", "tank", "power"], last="7d", every="1h")
    assert isinstance(f, enca.Frame)
    assert f["tank"].values[0] == 120.0
    assert f["power"].values[0] == pytest.approx(2.0)  # W → kW
    apps = sorted(
        re.search(r'r\.appliance == "([^"]+)"', q).group(1)
        for q in fake.queries
        if "aggregateWindow" in q
    )
    assert apps == [HP, TANK]  # tank reading queried on the tank device
    tank_q = next(q for q in fake.queries if TANK in q and "aggregateWindow" in q)
    assert 'r.entity == "data/t1"' in tank_q and "data/t9" not in tank_q
    assert all(q.startswith("telemetry_10m(") for q in fake.queries if "aggregateWindow" in q)
    df = f.to_dataframe()
    assert list(df.columns) == ["ground_loop_in", "tank", "power"]
    assert len(df) == 5 and df.attrs["units"]["power"] == "kW"
    assert len(f.points) == 5
    assert f.to_csv().splitlines()[0] == "time,ground_loop_in (°F),tank (°F),power (kW)"


def test_history_derived_subcooling(fake: FakeApi, client: enca.Client) -> None:
    fake.history = _flat(0.0)
    s = client.site().heat_pump.history("subcooling", last="6h", every="1h")
    assert s.values == [5.0] * 5
    assert 'r.entity == "data/refr_cond_temp" or r.entity == "data/t6"' in fake.queries[-1]


def test_history_bad_name_and_too_many_points(client: enca.Client) -> None:
    hp = client.site().heat_pump
    with pytest.raises(enca.NotFound, match="Valid names"):
        hp.history("cops")
    with pytest.raises(enca.QueryError, match="points"):
        hp.history("cop", last="365d", every="1m")


def test_query_escape_hatch(fake: FakeApi, client: enca.Client) -> None:
    with pytest.raises(enca.QueryError):
        client.query('from(bucket: "x")')
    assert fake.queries == []
    rows = client.query(
        'telemetry(start: -1h) |> filter(fn: (r) => r.appliance == "hp_000001") |> last()'
    )
    assert rows and rows[0]["appliance"] == HP


def test_live_values_are_reused_briefly(fake: FakeApi, client: enca.Client) -> None:
    site = client.site()
    site.heat_pump.now()
    site.tank.now()
    site.thermostats[0].now()
    assert len([q for q in fake.queries if "(data|remote)" in q]) == 1
    site.live(refresh=True)
    assert len([q for q in fake.queries if "(data|remote)" in q]) == 2
    assert timedelta(hours=24)
