"""Internal: the HTTP layer, a polite enca-api client.

* at most ``max_concurrency`` requests in flight (default 4; the API allows
  50 concurrent queries for everyone together);
* retries with exponential backoff and jitter on 429, 5xx and network
  timeouts, honouring ``Retry-After``;
* one token refresh and retry on 401;
* errors mapped to :mod:`enca.errors`, never containing tokens.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from ._auth import TokenProvider
from ._flux import check_blocklist, parse_csv
from .errors import ApiError, AuthError, NotFound, QueryError

log = logging.getLogger("enca")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_EXC = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

#: The API stops a query at 50 s; give it a little longer before giving up.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class RateLimit:
    """At most ``per_minute`` requests in any 60 s window (sliding)."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    return
                delay = 60.0 - (now - self._stamps[0])
            self._sleep(max(delay, 0.01))


class Api:
    def __init__(
        self,
        base_url: str,
        tokens: TokenProvider,
        *,
        max_concurrency: int = 4,
        retries: int = 4,
        backoff: float = 0.5,
        max_backoff: float = 20.0,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "enca-python",
        rate_limit: RateLimit | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": user_agent},
        )
        self._tokens = tokens
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self.max_concurrency = max_concurrency
        self._retries = retries
        self._backoff = backoff
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._rate = rate_limit
        self._pat = bool(getattr(tokens, "is_pat", False))

    def close(self) -> None:
        self._http.close()

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    return min(max(float(ra), 0.0), 60.0)
                except ValueError:
                    pass
        base: float = min(self._max_backoff, self._backoff * float(1 << attempt))
        return base * (0.5 + random.random() / 2)

    def request(
        self,
        method: str,
        path: str,
        *,
        content: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            h = dict(headers or {})
            h["Authorization"] = f"Bearer {self._tokens.token()}"
            if self._rate is not None:
                self._rate.wait()
            try:
                with self._sem:
                    resp = self._http.request(method, path, content=content, headers=h)
            except _RETRY_EXC as e:
                if attempt < self._retries:
                    d = self._delay(attempt, None)
                    log.info("%s %s: %s, retrying in %.1fs", method, path, type(e).__name__, d)
                    self._sleep(d)
                    attempt += 1
                    continue
                raise ApiError(f"{method} {path} failed: {type(e).__name__}") from None
            log.debug("%s %s -> %s", method, path, resp.status_code)
            if resp.status_code == 401 and not refreshed and self._tokens.refresh():
                refreshed = True
                continue
            if resp.status_code in RETRY_STATUS and attempt < self._retries:
                d = self._delay(attempt, resp)
                log.info("%s %s -> %s, retrying in %.1fs", method, path, resp.status_code, d)
                self._sleep(d)
                attempt += 1
                continue
            self._raise_for(resp, method, path)
            return resp

    def _raise_for(self, resp: httpx.Response, method: str, path: str) -> None:
        s = resp.status_code
        if s < 400:
            return
        detail = _detail(resp)
        if s == 401 and self._pat:
            raise AuthError(
                "The API refused your personal access token: it was revoked or has "
                "expired. Create a new one in the app (Über Geek Mode → API access)."
            )
        if s == 403 and self._pat:
            raise AuthError(
                f"Personal access tokens are read-only; {method} {path} isn't allowed{detail}."
            )
        if s in (401, 403):
            raise AuthError(
                f"The API refused the sign-in (HTTP {s}){detail}. "
                "Run `enca login` again, or check the token."
            )
        if s == 404:
            raise NotFound(f"{path} was not found (or is not yours){detail}.")
        if path.startswith("/query") and s in (400, 413, 422):
            raise QueryError(f"The API rejected the query (HTTP {s}){detail}")
        if s == 429:
            raise ApiError(f"Rate limited by the API; try again shortly{detail}.", s)
        raise ApiError(f"{method} {path} failed with HTTP {s}{detail}", s)

    def get_json(self, path: str) -> Any:
        resp = self.request("GET", path, headers={"Accept": "application/json"})
        try:
            return resp.json()
        except ValueError:
            raise ApiError(f"GET {path} returned something that isn't JSON.") from None

    def query(self, flux: str) -> list[dict[str, str]]:
        """Run Flux and return the rows of its annotated-CSV answer."""
        check_blocklist(flux)
        if len(flux.encode()) > 1_000_000:
            raise QueryError("Query is larger than the API's 1 MB limit.")
        resp = self.request(
            "POST",
            "/query",
            content=flux.encode(),
            headers={"Content-Type": "application/vnd.flux", "Accept": "application/csv"},
        )
        return parse_csv(resp.text)


def _detail(resp: httpx.Response) -> str:
    text = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            text = str(body.get("message") or body.get("error") or body.get("detail") or "")
    except ValueError:
        text = resp.text
    text = " ".join(text.split())[:300]
    return f": {text}" if text else ""
