"""A fake enca-api (httpx.MockTransport). No test talks to a real service."""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

import enca

SITE = "s_example"
HP = "hp_000001"
TANK = "tk_000001"


def make_jwt(**claims: Any) -> str:
    def enc(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def make_csv(rows: list[dict[str, str]], result: str = "_result") -> str:
    """Annotated CSV like Influx sends, one table per distinct column set."""
    if not rows:
        return ""
    out = []
    groups: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for r in rows:
        groups.setdefault(tuple(r), []).append(r)
    for i, (cols, rs) in enumerate(groups.items()):
        out.append("#datatype,string,long," + ",".join("string" for _ in cols))
        out.append("#group,false,false," + ",".join("false" for _ in cols))
        out.append("#default," + result + ",," + ",".join("" for _ in cols))
        out.append(",result,table," + ",".join(cols))
        for r in rs:
            res = r.get("result", result)
            out.append(f",{res},{i}," + ",".join(_cell(r[c]) for c in cols))
        out.append("")
    return "\r\n".join(out) + "\r\n"


def _cell(v: str) -> str:
    return '"' + v.replace('"', '""') + '"' if ("," in v or '"' in v) else v


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FakeApi:
    devices: list[str] = field(
        default_factory=lambda: [HP, TANK, "tst_000001", "tst_000002", "controller"]
    )
    subtypes: dict[str, str] = field(default_factory=lambda: {HP: "heat-pump", TANK: "tank"})
    config: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            HP: {"assign/tank": TANK, "info/subtype": "heat-pump"},
            "tst_000001": {
                "assign/name": "Living room",
                "assign/type": "fa",
                "info/subtype": "thermostat",
            },
            "tst_000002": {"assign/type": "hw", "info/subtype": "thermostat"},
            "controller": {"assign/dedicated_hw": "0", "info/subtype": "controller"},
        }
    )
    live: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            HP: {
                "data/cop": "3.8",
                "data/total_power": "2400",
                "data/t9": "45.2",
                "data/t10": "41.0",
                "data/q1": "9.2",
                "data/t13": "68.0",
                "data/t14": "95.5",
                "data/fan": "62",
                "data/p1": "120",
                "data/p2": "380",
                "data/refr_cond_temp": "105.0",
                "data/t6": "98.5",
                "data/refr_sh_final": "8.4",
                "data/mode": '"forced_air"',
                "data/hw_heat_rejection": "6824.28",
                "bus/inverter/data/compressor_running_speed": "3600",
                # a stale t1 the heat pump must NOT be read for
                "data/t1": "1.0",
            },
            TANK: {"data/t1": "124.5", "data/setpoint": "125", "data/heating_relay": "1"},
            "tst_000001": {
                "data/temp": "70.5",
                "data/setpoint": "72",
                "data/mode": "heat",
                "data/humidity": "41",
            },
            "tst_000002": {"data/temp": "66", "data/setpoint": "65", "data/mode": "heat"},
        }
    )
    history: Callable[[str, str, datetime, datetime, timedelta], list[float]] | None = None
    queries: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    query_handler: Callable[[str], httpx.Response] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/sites":
            return httpx.Response(200, json={"sites": [SITE]})
        m = re.fullmatch(r"/sites/([^/]+)/devices", path)
        if m:
            if m.group(1) != SITE:
                return httpx.Response(404, json={"message": "no such site"})
            return httpx.Response(200, json={"devices": self.devices, "subtypes": self.subtypes})
        if path == "/whoami":
            return httpx.Response(200, json={"sub": "user-1"})
        if path == "/query":
            flux = request.content.decode()
            enca._flux.check_blocklist(flux)  # every query we build passes
            self.queries.append(flux)
            if self.query_handler:
                return self.query_handler(flux)
            return httpx.Response(200, text=self.answer(flux))
        return httpx.Response(404)

    def answer(self, flux: str) -> str:
        now = datetime.now(timezone.utc)
        apps = re.findall(r'r\.appliance == "([^"]+)"', flux)
        if "(info|assign)" in flux:
            rows = [
                {"appliance": a, "entity": e, "_value": v, "_time": iso(now)}
                for a in apps
                for e, v in self.config.get(a, {}).items()
            ]
            return make_csv(rows)
        if "last()" in flux:
            rows = [
                {
                    "appliance": a,
                    "entity": e,
                    "_value": v,
                    "_time": iso(now - timedelta(seconds=30)),
                }
                for a in apps
                for e, v in self.live.get(a, {}).items()
            ]
            return make_csv(rows)
        if (
            "aggregateWindow(every:" in flux
            and 'keep(columns: ["entity", "_time", "_value"])' in flux
        ):
            ents = re.findall(r'r\.entity == "([^"]+)"', flux)
            start = enca._times.to_utc(re.search(r"start: (\S+),", flux).group(1))
            stop = enca._times.to_utc(re.search(r"stop: (\S+)\)", flux).group(1))
            every = enca._times.parse_duration(re.search(r"every: (\w+),", flux).group(1))
            rows = []
            for e in ents:
                vals = self.history(apps[0], e, start, stop, every) if self.history else []
                for i, v in enumerate(vals):
                    rows.append(
                        {"entity": e, "_time": iso(start + every * (i + 1)), "_value": repr(v)}
                    )
            return make_csv(rows)
        return ""


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client(fake: FakeApi) -> enca.Client:
    c = enca.Client(
        token=make_jwt(exp=time.time() + 3600),
        api_url="https://api.example.test",
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda s: None,
    )
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("ENCA_TOKEN", raising=False)
    monkeypatch.setenv("ENCA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ENCA_API_URL", "https://api.example.test")
    monkeypatch.setenv("ENCA_AUTH_URL", "https://auth.example.test")
