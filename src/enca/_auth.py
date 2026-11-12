"""Sign-in: browser OAuth (code + PKCE), token cache, refresh, raw tokens.

Recommended: a personal access token (``enca_pat_...``, made in the app
under Über Geek Mode → API access) passed as ``Client(token=...)`` or
``ENCA_TOKEN``. It is long-lived, read-only, and never refreshed here.

FALLBACK / STOPGAP: the browser flow redirects to ``http://localhost:3000/``,
the developer callback registered on the app's sign-in client.

Tokens are never logged, printed or put in exception messages.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import selectors
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import _config
from .errors import AuthError

log = logging.getLogger("enca")

#: Refresh tokens last 30 days (no rotation).
REFRESH_LIFETIME = 30 * 86400
#: Refresh the ID token this long before it expires.
EXPIRY_MARGIN = 120


# ---- PKCE -------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A (code_verifier, S256 code_challenge) pair per RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 chars of [A-Za-z0-9_-]
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(
    *,
    challenge: str,
    state: str,
    identity_provider: str | None = None,
    auth_url: str | None = None,
    client_id: str = _config.CLIENT_ID,
    redirect_uri: str = _config.REDIRECT_URI,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _config.SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if identity_provider:
        params["identity_provider"] = identity_provider
    return f"{auth_url or _config.auth_url()}/oauth2/authorize?{urlencode(params)}"


# ---- Token set and cache ----------------------------------------------------


def jwt_claims(token: str) -> dict[str, Any]:
    """The (unverified) claims of a JWT; {} if it isn't one. Used only to read
    ``exp`` and ``email`` locally; the API does the real verification."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class TokenSet:
    id_token: str
    refresh_token: str | None
    expires_at: float
    refresh_expires_at: float | None = None

    def __repr__(self) -> str:  # never show tokens
        return f"TokenSet(expires_at={self.expires_at:.0f})"

    @classmethod
    def from_response(
        cls, body: dict[str, Any], *, previous_refresh: str | None = None, now: float | None = None
    ) -> TokenSet:
        now = time.time() if now is None else now
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise AuthError("Sign-in server returned no ID token.")
        exp = jwt_claims(id_token).get("exp")
        expires_at = (
            float(exp)
            if isinstance(exp, (int, float))
            else now + float(body.get("expires_in", 3600))
        )
        new_refresh = body.get("refresh_token")
        refresh = new_refresh if isinstance(new_refresh, str) else previous_refresh
        return cls(
            id_token=id_token,
            refresh_token=refresh,
            expires_at=expires_at,
            refresh_expires_at=(now + REFRESH_LIFETIME) if isinstance(new_refresh, str) else None,
        )

    def valid(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) < self.expires_at - EXPIRY_MARGIN


class TokenCache:
    """Tokens on disk, readable only by you (0600 file in a 0700 directory)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _config.token_cache_path()

    def load(self) -> TokenSet | None:
        try:
            data = json.loads(self.path.read_text())
            return TokenSet(**{k: data.get(k) for k in TokenSet.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(tokens), f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


# ---- Token endpoint ---------------------------------------------------------


def _token_request(http: httpx.Client, auth_url: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = http.post(
            f"{auth_url}/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as e:
        raise AuthError(f"Could not reach the sign-in server: {type(e).__name__}") from None
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", "")
        except ValueError:
            err = ""
        raise AuthError(f"Sign-in server refused the request (HTTP {resp.status_code} {err}).")
    body = resp.json()
    if not isinstance(body, dict):
        raise AuthError("Sign-in server sent an unexpected answer.")
    return body


def exchange_code(
    http: httpx.Client,
    *,
    code: str,
    verifier: str,
    auth_url: str | None = None,
    client_id: str = _config.CLIENT_ID,
    redirect_uri: str = _config.REDIRECT_URI,
) -> TokenSet:
    body = _token_request(
        http,
        auth_url or _config.auth_url(),
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    return TokenSet.from_response(body)


def refresh_tokens(
    http: httpx.Client,
    refresh_token: str,
    *,
    auth_url: str | None = None,
    client_id: str = _config.CLIENT_ID,
) -> TokenSet:
    body = _token_request(
        http,
        auth_url or _config.auth_url(),
        {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token},
    )
    return TokenSet.from_response(body, previous_refresh=refresh_token)


# ---- Browser sign-in --------------------------------------------------------

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Signed in</title>"
    b"<body style='font-family:sans-serif;margin:3em'>"
    b"<h2>Signed in to Energy Catalyst</h2><p>You can close this tab.</p>"
)


def _wait_for_redirect(port: int, timeout: float) -> dict[str, str]:
    """Serve one OAuth redirect on localhost:``port`` (IPv4 and IPv6)."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path != "/" or not ("code" in qs or "error" in qs):
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # the URL carries the code; never log it

    servers: list[HTTPServer] = []
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        cls = type("_Server", (HTTPServer,), {"address_family": family})
        try:
            servers.append(cls((host, port), Handler))
        except OSError:
            continue
    if not servers:
        raise AuthError(
            f"Can't listen on localhost:{port} for the sign-in redirect; "
            "is another program (a dev server?) using that port?"
        )
    sel = selectors.DefaultSelector()
    for s in servers:
        sel.register(s.socket, selectors.EVENT_READ, s)
    deadline = time.monotonic() + timeout
    try:
        while not result:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AuthError("Timed out waiting for the browser sign-in.")
            for key, _ in sel.select(left):
                key.data.handle_request()
    finally:
        sel.close()
        for s in servers:
            s.server_close()
    return result


def _parse_pasted(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url.strip()).query).items()}


def login(
    identity_provider: str | None = None,
    *,
    open_browser: bool = True,
    manual: bool = False,
    timeout: float = 300,
    cache_path: Path | None = None,
    auth_url: str | None = None,
    http: httpx.Client | None = None,
    _wait: Callable[[int, float], dict[str, str]] | None = None,
    _print: Callable[[str], None] = print,
    _input: Callable[[str], str] = input,
) -> str | None:
    """Sign in through the browser and cache the tokens.

    ``identity_provider`` skips the chooser: ``"Google"`` or
    ``"SignInWithApple"``. With ``manual=True`` (e.g. on a headless machine)
    the page at localhost won't load; copy the address from the browser's
    address bar and paste it when asked.

    Returns the signed-in email when the token carries one.

    Fallback for when you don't use a personal access token. Stopgap: it uses
    the app client's ``http://localhost:3000/`` developer callback, so port
    3000 must be free.
    """
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(
        challenge=challenge, state=state, identity_provider=identity_provider, auth_url=auth_url
    )
    _print("Sign in to Energy Catalyst in your browser:\n  " + url)
    if manual:
        reply = _parse_pasted(
            _input("After signing in, paste the full address the browser ended up on: ")
        )
    else:
        waiting: dict[str, Any] = {}

        def run() -> None:
            try:
                waiting["r"] = (_wait or _wait_for_redirect)(_config.REDIRECT_PORT, timeout)
            except BaseException as e:
                waiting["e"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        if open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        t.join()
        if "e" in waiting:
            raise waiting["e"]
        reply = waiting["r"]
    if reply.get("state") != state:
        raise AuthError("Sign-in reply didn't match this request (state mismatch); try again.")
    if "error" in reply:
        raise AuthError(f"Sign-in failed: {reply.get('error_description') or reply['error']}")
    code = reply.get("code")
    if not code:
        raise AuthError("Sign-in reply carried no code.")
    own = http is None
    client = http or httpx.Client(timeout=30)
    try:
        tokens = exchange_code(client, code=code, verifier=verifier, auth_url=auth_url)
    finally:
        if own:
            client.close()
    TokenCache(cache_path).save(tokens)
    email = jwt_claims(tokens.id_token).get("email")
    return email if isinstance(email, str) else None


def logout(cache_path: Path | None = None) -> bool:
    """Forget the cached sign-in. True if there was one."""
    return TokenCache(cache_path).clear()


# ---- Providers the HTTP layer uses ------------------------------------------


class TokenProvider(Protocol):
    def token(self) -> str: ...

    def refresh(self) -> bool:
        """Get a fresh token after a 401; True if retrying makes sense."""
        ...


PAT_PREFIX = "enca_pat_"


def is_pat(token: str) -> bool:
    """A personal access token (``enca_pat_...``): long-lived, read-only."""
    return token.strip().startswith(PAT_PREFIX)


class StaticToken:
    """A token passed in as-is: a personal access token (``enca_pat_...``,
    the recommended way) or a raw ID token. Never refreshed; a PAT's expiry
    is not decoded (the API decides)."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise AuthError("Empty token.")
        self._token = token.strip()
        self.is_pat = is_pat(self._token)

    def __repr__(self) -> str:
        return "StaticToken(enca_pat_***)" if self.is_pat else "StaticToken(***)"

    def token(self) -> str:
        return self._token

    def refresh(self) -> bool:
        return False


class CachedLogin:
    """Tokens from the cache, refreshed with the refresh token; re-runs the
    browser sign-in (when ``interactive``) once the refresh token expires."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        cache: TokenCache | None = None,
        auth_url: str | None = None,
        interactive: bool = False,
        login_fn: Callable[[], object] | None = None,
    ) -> None:
        self._http = http
        self._cache = cache or TokenCache()
        self._auth_url = auth_url
        self._interactive = interactive
        self._login_fn = login_fn or (lambda: login(cache_path=self._cache.path, auth_url=auth_url))
        self._tokens: TokenSet | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"CachedLogin(cache={str(self._cache.path)!r})"

    def token(self) -> str:
        with self._lock:
            if self._tokens is None:
                self._tokens = self._cache.load()
            if self._tokens is not None and self._tokens.valid():
                return self._tokens.id_token
            self._renew()
            assert self._tokens is not None
            return self._tokens.id_token

    def refresh(self) -> bool:
        with self._lock:
            try:
                self._renew(force=True)
            except AuthError:
                return False
            return True

    def _renew(self, force: bool = False) -> None:
        t = self._tokens
        if t is not None and t.refresh_token:
            try:
                fresh = refresh_tokens(self._http, t.refresh_token, auth_url=self._auth_url)
                fresh.refresh_expires_at = t.refresh_expires_at
                self._tokens = fresh
                self._cache.save(fresh)
                log.debug("ID token refreshed")
                return
            except AuthError:
                log.info("Refresh token rejected (expired after 30 days, or revoked)")
        if not self._interactive:
            raise AuthError(
                "Not signed in. Pass a personal access token (Client(token=...) or "
                "ENCA_TOKEN; create one in the app under Über Geek Mode → API access), "
                "or run `enca login` for the browser sign-in."
            )
        self._login_fn()
        self._tokens = self._cache.load()
        if self._tokens is None:
            raise AuthError("Sign-in did not produce tokens.")
