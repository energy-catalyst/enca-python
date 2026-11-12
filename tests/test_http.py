import threading
import time

import httpx
import pytest

import enca
from conftest import FakeApi, make_jwt
from enca._http import RateLimit

PAT = "enca_pat_" + "A" * 43


def _client(handler, token: str | None = None, sleeps: list[float] | None = None, **kw):
    return enca.Client(
        token=token or make_jwt(exp=time.time() + 3600),
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        **kw,
    )


def test_retries_429_honouring_retry_after(fake: FakeApi) -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return fake.handler(req)

    sleeps: list[float] = []
    with _client(h, sleeps=sleeps) as c:
        assert c.sites()
    assert sleeps == [3.0, 3.0]


def test_backoff_grows_on_5xx_then_gives_up() -> None:
    sleeps: list[float] = []
    with (
        _client(lambda r: httpx.Response(503), sleeps=sleeps, retries=3) as c,
        pytest.raises(enca.ApiError) as e,
    ):
        c.sites()
    assert e.value.status == 503
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[2] <= 20
    assert all(s > 0 for s in sleeps)


def test_retries_timeouts(fake: FakeApi) -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return fake.handler(req)

    with _client(h) as c:
        assert c.sites()
    assert n["i"] == 2


def test_error_mapping(fake: FakeApi) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/query":
            return httpx.Response(400, json={"message": "error in query"})
        if req.url.path == "/whoami":
            return httpx.Response(403)
        return fake.handler(req)

    with _client(h) as c:
        with pytest.raises(enca.QueryError, match="error in query"):
            c.query("telemetry(start: -1h)")
        with pytest.raises(enca.AuthError):
            c.whoami()
        with pytest.raises(enca.NotFound):
            c._api.get_json("/nowhere")


def test_pat_401_says_revoked_or_expired() -> None:
    with (
        _client(lambda r: httpx.Response(401), token=PAT) as c,
        pytest.raises(enca.AuthError, match="revoked or has expired") as e,
    ):
        c.sites()
    assert PAT not in str(e.value)


def test_pat_defaults_to_two_in_flight() -> None:
    with _client(lambda r: httpx.Response(200), token=PAT) as c:
        assert c.max_concurrency == 2
    with _client(lambda r: httpx.Response(200)) as c:
        assert c.max_concurrency == 4


def test_concurrency_limit_is_respected(fake: FakeApi) -> None:
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def h(req: httpx.Request) -> httpx.Response:
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return httpx.Response(200, text="")

    with _client(h, max_concurrency=3) as c:
        c._map(lambda i: c.query("telemetry(start: -1h)"), range(12))
    assert state["max"] <= 3


def test_rate_limit_window() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    rl = RateLimit(3, clock=lambda: clock["t"], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == []
    rl.wait()
    assert slept == [60.0]
