"""The entry point: :class:`Client`."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

import httpx

from . import _config
from ._auth import CachedLogin, StaticToken, TokenCache, TokenProvider
from ._http import DEFAULT_TIMEOUT, Api, RateLimit
from .errors import NotFound
from .models import Site

T = TypeVar("T")
R = TypeVar("R")

__version__ = "0.1.0"

#: Requests in flight with a browser sign-in.
DEFAULT_CONCURRENCY = 4
#: The API's per-token limits for personal access tokens.
PAT_CONCURRENCY = 2
PAT_PER_MINUTE = 60


class Client:
    """Your Energy Catalyst account.

    Credentials, first match wins:

    1. ``token=...``: a personal access token (``enca_pat_...``, created in
       the app under Über Geek Mode → API access). The recommended way.
    2. the ``ENCA_TOKEN`` environment variable (same thing).
    3. Fallback: the browser sign-in cached by :func:`enca.login` /
       ``enca login``, refreshed automatically. With ``interactive=True``
       (default when run from a terminal) an expired or missing sign-in opens
       the browser again.

    With a personal access token the client keeps to the API's per-token
    limits: 2 requests at a time and at most 60 requests a minute.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str | None = None,
        auth_url: str | None = None,
        interactive: bool | None = None,
        max_concurrency: int | None = None,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        retries: int = 4,
        cache_path: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        token = token or os.environ.get("ENCA_TOKEN") or None
        self._auth_http = httpx.Client(timeout=30, transport=transport)
        tokens: TokenProvider
        rate: RateLimit | None = None
        if token:
            static = StaticToken(token)
            tokens = static
            if static.is_pat:
                rate = RateLimit(PAT_PER_MINUTE, sleep=sleep or time.sleep)
                if max_concurrency is None:
                    max_concurrency = PAT_CONCURRENCY
        else:
            if interactive is None:
                interactive = sys.stdin.isatty() and sys.stdout.isatty()
            tokens = CachedLogin(
                self._auth_http,
                cache=TokenCache(cache_path),
                auth_url=auth_url,
                interactive=interactive,
            )
        if max_concurrency is None:
            max_concurrency = DEFAULT_CONCURRENCY
        kw: dict[str, Any] = {}
        if sleep is not None:
            kw["sleep"] = sleep
        self._api = Api(
            (api_url or _config.api_url()).rstrip("/"),
            tokens,
            max_concurrency=max_concurrency,
            retries=retries,
            timeout=timeout,
            transport=transport,
            user_agent=f"enca-python/{__version__}",
            rate_limit=rate,
            **kw,
        )
        self._pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="enca")
        self._sites: list[Site] | None = None

    def __repr__(self) -> str:
        return "Client()"

    @property
    def max_concurrency(self) -> int:
        return self._api.max_concurrency

    def close(self) -> None:
        self._pool.shutdown(wait=False)
        self._api.close()
        self._auth_http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        """Run ``fn`` over ``items``, at most ``max_concurrency`` at a time."""
        items = list(items)
        if len(items) <= 1:
            return [fn(i) for i in items]
        return list(self._pool.map(fn, items))

    # ---- account

    def whoami(self) -> Any:
        """Who the API thinks you are."""
        return self._api.get_json("/whoami")

    def sites(self, refresh: bool = False) -> list[Site]:
        """Your sites."""
        if self._sites is None or refresh:
            body = self._api.get_json("/sites")
            raw = body.get("sites", []) if isinstance(body, dict) else body
            out: list[Site] = []
            for s in raw if isinstance(raw, list) else []:
                if isinstance(s, str):
                    out.append(Site(self, s))
                elif isinstance(s, dict):
                    sid = s.get("id") or s.get("site") or s.get("site_id")
                    if isinstance(sid, str):
                        name = s.get("name")
                        out.append(Site(self, sid, name=name if isinstance(name, str) else None))
            self._sites = out
        return list(self._sites)

    def site(self, site_id: str | None = None) -> Site:
        """One site by id; without an id, your only site."""
        sites = self.sites()
        if site_id is None:
            if len(sites) == 1:
                return sites[0]
            raise NotFound(
                f"You have {len(sites)} sites; name one: {', '.join(s.id for s in sites) or '-'}"
            )
        for s in sites:
            if s.id == site_id:
                return s
        raise NotFound(
            f"No site {site_id!r}. Your sites: {', '.join(s.id for s in sites) or 'none'}"
        )

    # ---- advanced

    def query(self, flux: str) -> list[dict[str, str]]:
        """Advanced: run your own Flux and get the rows (column → text).

        Start from ``telemetry(start:, stop:)`` or ``telemetry_1m``/``_10m``/
        ``_1h`` (scoped to your sites by the API). Queries containing the words
        from, import, to, buckets, cardinality or wideTo, anywhere, are
        refused (:class:`enca.QueryError`) before they're sent.
        """
        return self._api.query(flux)
