from datetime import datetime, timedelta, timezone

import pytest

from enca import QueryError, _flux
from enca._times import flux_duration, parse_duration, parse_time, resolve_range

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("span", "ago", "tier"),
    [
        (timedelta(hours=1), timedelta(0), "telemetry"),
        (timedelta(hours=3), timedelta(minutes=4), "telemetry"),
        (timedelta(hours=3), timedelta(minutes=10), "telemetry_1m"),
        (timedelta(hours=4), timedelta(0), "telemetry_1m"),
        (timedelta(hours=50), timedelta(0), "telemetry_1m"),
        (timedelta(hours=51), timedelta(0), "telemetry_10m"),
        (timedelta(days=35), timedelta(0), "telemetry_10m"),
        (timedelta(days=36), timedelta(0), "telemetry_1h"),
    ],
)
def test_tier_for(span: timedelta, ago: timedelta, tier: str) -> None:
    stop = NOW - ago
    assert _flux.tier_for(stop - span, stop, NOW) == tier


def test_step_for_keeps_about_200_points() -> None:
    assert _flux.step_for(timedelta(hours=3)) == timedelta(minutes=1)
    assert _flux.step_for(timedelta(hours=24)) == timedelta(minutes=15)
    assert _flux.step_for(timedelta(days=7)) == timedelta(hours=1)
    assert _flux.step_for(timedelta(days=30)) == timedelta(hours=6)


def test_tier_steps_down_for_fine_every() -> None:
    s = NOW - timedelta(days=7)
    assert _flux.tier_for_step(s, NOW, NOW, timedelta(hours=1)) == "telemetry_10m"
    assert _flux.tier_for_step(s, NOW, NOW, timedelta(minutes=5)) == "telemetry_1m"
    s = NOW - timedelta(days=60)
    assert _flux.tier_for_step(s, NOW, NOW, timedelta(hours=6)) == "telemetry_1h"
    assert _flux.tier_for_step(s, NOW, NOW, timedelta(minutes=10)) == "telemetry_10m"
    with pytest.raises(QueryError):
        _flux.tier_for_step(s, NOW, NOW, timedelta(seconds=30))


def test_series_flux_raw_shape() -> None:
    f = _flux.series_flux(
        tier="telemetry",
        site="s_example",
        device="hp_000001",
        entities=["data/cop", "data/t9"],
        start=NOW - timedelta(hours=1),
        stop=NOW,
        every=timedelta(minutes=1),
    )
    assert f.startswith("telemetry(start: 2026-11-12T11:00:00Z, stop: 2026-11-12T12:00:00Z)")
    assert 'r._field == "value"' in f
    assert "|> toFloat()" in f
    assert 'r.entity == "data/cop" or r.entity == "data/t9"' in f
    assert "aggregateWindow(every: 1m, fn: mean, createEmpty: false)" in f
    assert 'r.site == "s_example" and r.appliance == "hp_000001"' in f


@pytest.mark.parametrize("tier", ["telemetry_1m", "telemetry_10m", "telemetry_1h"])
def test_series_flux_downsampled_shape(tier: str) -> None:
    f = _flux.series_flux(
        tier=tier,
        site="s_example",
        device="hp_000001",
        entities=["data/cop"],
        start=NOW - timedelta(days=3),
        stop=NOW,
        every=timedelta(hours=1),
    )
    assert f.startswith(f"{tier}(start:")
    assert 'r._field == "mean"' in f
    assert "toFloat" not in f


def test_no_built_query_trips_the_blocklist() -> None:
    from enca.readings import REGISTRY

    entities = sorted({r.entity for r in REGISTRY.values() if r.entity})
    for tier in ("telemetry", "telemetry_1m", "telemetry_10m", "telemetry_1h"):
        for every in ("1m", "10m", "1h", "6h", "1d"):
            _flux.check_blocklist(
                _flux.series_flux(
                    tier=tier,
                    site="s_example",
                    device="hp_000001",
                    entities=entities,
                    start=NOW - timedelta(days=2),
                    stop=NOW,
                    every=parse_duration(every),
                )
            )
    for config in (False, True):
        _flux.check_blocklist(
            _flux.latest_flux(
                site="s_example",
                devices=["hp_000001", "tk_000001", "controller"],
                start=NOW - timedelta(days=1),
                stop=NOW,
                config=config,
            )
        )
    _flux.check_blocklist(
        _flux.energy_hours_flux(
            site="s_example", device="hp_000001", start=NOW - timedelta(days=31), stop=NOW
        )
    )


@pytest.mark.parametrize("word", ["from", "import", "to", "buckets", "cardinality", "wideTo"])
def test_blocklist_refuses(word: str) -> None:
    with pytest.raises(QueryError, match=word):
        _flux.check_blocklist(f'telemetry(start: -1h) |> filter(fn: (r) => r.x == "{word}")')


def test_blocklist_catches_comments_and_case_but_not_substrings() -> None:
    with pytest.raises(QueryError):
        _flux.check_blocklist("telemetry(start: -1h)\n// data From the tank")
    _flux.check_blocklist("telemetry(start: -1h) |> toFloat() |> fromage() |> wideToo()")


def test_ids_cannot_break_out_of_strings() -> None:
    with pytest.raises(ValueError):
        _flux.series_flux(
            tier="telemetry_1m",
            site='s_x" or true or "',
            device="hp_1",
            entities=["data/cop"],
            start=NOW - timedelta(hours=5),
            stop=NOW,
            every=timedelta(hours=1),
        )


def test_annotated_csv_parsing() -> None:
    text = (
        "#datatype,string,long,dateTime:RFC3339,double,string\r\n"
        "#group,false,false,false,false,true\r\n"
        "#default,_result,,,,\r\n"
        ",result,table,_time,_value,entity\r\n"
        ",,0,2026-11-12T10:00:00Z,3.5,data/cop\r\n"
        ",,0,2026-11-12T10:10:00.123456789Z,3.7,data/cop\r\n"
        "\r\n"
        "#datatype,string,long,string,string\r\n"
        "#group,false,false,true,false\r\n"
        "#default,mode,,,\r\n"
        ",result,table,entity,_value\r\n"
        ',mode,1,data/mode,"forced_air, quoted"\r\n'
    )
    rows = _flux.parse_csv(text)
    assert len(rows) == 3
    assert rows[0] == {
        "result": "",
        "table": "0",
        "_time": "2026-11-12T10:00:00Z",
        "_value": "3.5",
        "entity": "data/cop",
    }
    assert rows[2]["_value"] == "forced_air, quoted"
    assert rows[2]["result"] == "mode"
    t = parse_time(rows[1]["_time"])
    assert t is not None and t.microsecond == 123456 and t.tzinfo is not None


def test_csv_error_table_raises() -> None:
    with pytest.raises(QueryError, match="bad thing"):
        _flux.parse_csv(",error,reference\r\n,bad thing,\r\n")


def test_to_float() -> None:
    assert _flux.to_float('"45.5"') == 45.5
    assert _flux.to_float("forced_air") is None
    assert _flux.to_float("NaN") is None
    assert _flux.to_float("") is None


def test_durations_and_ranges() -> None:
    assert flux_duration(timedelta(days=1)) == "1d"
    assert flux_duration(timedelta(minutes=90)) == "90m"
    assert flux_duration(timedelta(seconds=30)) == "30s"
    with pytest.raises(ValueError):
        parse_duration("soon")
    s, e = resolve_range(None, None, "24h", now=NOW)
    assert e == NOW and e - s == timedelta(hours=24)
    s, e = resolve_range("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", None, now=NOW)
    assert e - s == timedelta(days=1)
    with pytest.raises(ValueError):
        resolve_range("2026-11-01T00:00:00Z", None, "1h", now=NOW)
