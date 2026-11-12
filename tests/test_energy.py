from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

import enca
from conftest import FakeApi, iso, make_csv
from enca import energy


def test_period_buckets() -> None:
    assert len(energy.period_buckets("month", date(2026, 2, 10))) == 28
    assert len(energy.period_buckets("year", date(2026, 5, 1))) == 12
    week = energy.period_buckets("week", date(2026, 11, 12))  # a Thursday
    assert len(week) == 7 and week[0][0].date() == date(2026, 11, 9)
    day = energy.period_buckets("day", date(2026, 6, 15))
    assert 23 <= len(day) <= 25
    assert all(e > s for s, e in day)
    assert (
        len(
            energy.range_buckets(
                datetime(2026, 11, 1, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 6, tzinfo=timezone.utc),
                "1h",
            )
        )
        >= 6
    )


def test_counters_give_exact_energy() -> None:
    s = datetime(2026, 11, 10, tzinfo=timezone.utc)
    h = energy.Hours()
    rows = []
    for i in range(-1, 24):  # an hour early for the baseline
        t = s + timedelta(hours=i)
        rows += [
            {
                "result": "counter",
                "entity": "energy/electric_wh",
                "_time": iso(t),
                "_value": str(10_000 + 1000 * (i + 1)),
            },
            {
                "result": "counter",
                "entity": "energy/electric_heating_wh",
                "_time": iso(t),
                "_value": str(5_000 + 1000 * (i + 1)),
            },
            {
                "result": "counter",
                "entity": "energy/heating_wh",
                "_time": iso(t),
                "_value": str(20_000 + 4000 * (i + 1)),
            },
        ]
    h.add_rows(rows)
    b = energy.compute_bucket(h, s, s + timedelta(days=1), 0.2)
    assert b.exact
    assert b.electricity_kwh == pytest.approx(24.0)
    assert b.heating_kwh == pytest.approx(96.0)
    assert b.cop == pytest.approx(4.0)
    assert b.cost == pytest.approx(4.8)


def test_estimate_without_counters() -> None:
    s = datetime(2026, 11, 10, tzinfo=timezone.utc)
    h = energy.Hours()
    rows = [{"result": "mode", "_time": iso(s - timedelta(hours=2)), "_value": '"forced_air"'}]
    for i in range(4):
        t = s + timedelta(hours=i)
        rows += [
            {"result": "power", "_time": iso(t), "_value": str(2000 * 60)},  # 2 kW for 1h
            {"result": "cop", "_time": iso(t), "_value": "3.5"},
        ]
    rows.append({"result": "mode", "_time": iso(s + timedelta(hours=3)), "_value": "cooling"})
    h.add_rows(rows)
    b = energy.compute_bucket(h, s, s + timedelta(hours=4), None)
    assert not b.exact
    assert b.electricity_kwh == pytest.approx(8.0)
    assert b.heating_kwh == pytest.approx(21.0)  # 3 heating hours × 2 kWh × 3.5
    assert b.cooling_kwh is None  # TODO: cooling estimate
    assert b.cop == pytest.approx(3.5)
    assert b.cost is None


def test_energy_report_end_to_end(fake: FakeApi, client: enca.Client) -> None:
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def answer(flux: str) -> httpx.Response:
        rows = []
        for i in range(-1, 48):
            t = start + timedelta(hours=i)
            rows.append(
                {
                    "result": "counter",
                    "entity": "energy/electric_wh",
                    "_field": "max",
                    "_time": iso(t),
                    "_value": str(1000 * (i + 1)),
                }
            )
        return httpx.Response(200, text=make_csv(rows))

    fake.query_handler = answer
    rep = client.site().heat_pump.energy(
        start=start, end=start + timedelta(days=2), every="1h", price=0.1
    )
    assert len(rep) >= 48
    assert rep.electricity_kwh == pytest.approx(48.0, abs=1.01)
    assert rep.cost == pytest.approx(rep.electricity_kwh * 0.1)
    q = fake.queries[0]
    for name in ("counter", "power", "cop", "mode"):
        assert f'yield(name: "{name}")' in q
    assert q.startswith("telemetry_1h(start: 2026-10-31T23:00:00Z") or "telemetry_1h(" in q
    df = rep.to_dataframe()
    assert "electricity_kwh" in df.columns


def test_site_energy_requires_heat_pump(fake: FakeApi, client: enca.Client) -> None:
    fake.devices = ["tk_000001"]
    with pytest.raises(enca.NotFound):
        client.site().energy("day")


def test_energy_args() -> None:
    with pytest.raises(ValueError):
        energy.period_buckets("decade", date.today())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        energy.range_buckets(datetime.now(timezone.utc), datetime.now(timezone.utc), "2h")
