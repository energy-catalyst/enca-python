import time

import httpx
import pytest

import enca
from conftest import FakeApi, make_jwt
from enca import cli


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, fake: FakeApi) -> FakeApi:
    real = enca.Client

    def make(*a, **kw):
        return real(
            token=make_jwt(exp=time.time() + 3600),
            transport=httpx.MockTransport(fake.handler),
            sleep=lambda s: None,
        )

    monkeypatch.setattr(cli, "Client", make)
    return fake


def test_readings(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["readings"]) == 0
    out = capsys.readouterr().out
    assert "ground_loop_in" in out and "Ground loop, from ground" in out


def test_sites_and_now(patched: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sites"]) == 0
    assert "hp_000001" in capsys.readouterr().out
    assert cli.main(["now"]) == 0
    out = capsys.readouterr().out
    assert "COP: 3.80" in out and "Living room: 70.5 °F" in out


def test_history_csv(patched: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    patched.history = lambda app, e, s, t, every: [3.0, 3.5]
    assert cli.main(["history", "cop", "--last", "24h", "--csv"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "time,cop"
    assert len(lines) == 3


def test_errors_are_friendly(patched: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["history", "copp"]) == 1
    assert "Did you mean 'cop'" in capsys.readouterr().err
