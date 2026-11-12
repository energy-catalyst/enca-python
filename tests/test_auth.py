import base64
import hashlib
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import enca
from conftest import FakeApi, make_jwt
from enca import _auth


def test_pkce_rfc7636_vector() -> None:
    v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert _auth.pkce_challenge(v) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_pair() -> None:
    v, c = _auth.pkce_pair()
    assert 43 <= len(v) <= 128
    assert set(v) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert _auth.pkce_pair()[0] != v


def test_authorize_url() -> None:
    u = urlparse(
        _auth.authorize_url(
            challenge="ch",
            state="st",
            identity_provider="Google",
            auth_url="https://auth.example.test",
        )
    )
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.path == "/oauth2/authorize"
    assert q == {
        "response_type": "code",
        "client_id": _auth._config.CLIENT_ID,
        "redirect_uri": "http://localhost:3000/",
        "scope": "openid email profile",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
        "state": "st",
        "identity_provider": "Google",
    }


class TokenEndpoint:
    def __init__(self, fake: FakeApi, *, refresh_ok: bool = True) -> None:
        self.fake = fake
        self.refresh_ok = refresh_ok
        self.calls: list[dict[str, str]] = []
        self.issued = make_jwt(exp=time.time() + 3600, email="owner@example.com", n=1)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.test":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.calls.append(form)
            if form["grant_type"] == "refresh_token" and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {"id_token": self.issued, "access_token": "a", "expires_in": 3600}
            if form["grant_type"] == "authorization_code":
                body["refresh_token"] = "refresh-2"
            return httpx.Response(200, json=body)
        return self.fake.handler(request)


def _cache(tmp_path: Path, **kw: object) -> Path:
    p = tmp_path / "tokens.json"
    data = {
        "id_token": make_jwt(exp=time.time() - 10),
        "refresh_token": "refresh-1",
        "expires_at": time.time() - 10,
        "refresh_expires_at": None,
    }
    data.update(kw)
    _auth.TokenCache(p).save(_auth.TokenSet(**data))  # type: ignore[arg-type]
    return p


def _client(handler, cache: Path, **kw) -> enca.Client:
    return enca.Client(
        cache_path=cache, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw
    )


def test_refresh_flow(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake)
    cache = _cache(tmp_path)
    with _client(ep, cache, interactive=False) as c:
        assert [s.id for s in c.sites()] == ["s_example"]
    assert ep.calls == [
        {
            "grant_type": "refresh_token",
            "client_id": _auth._config.CLIENT_ID,
            "refresh_token": "refresh-1",
        }
    ]
    assert fake.requests[-1].headers["Authorization"] == f"Bearer {ep.issued}"
    saved = _auth.TokenCache(cache).load()
    assert saved is not None and saved.id_token == ep.issued
    assert saved.refresh_token == "refresh-1"  # no rotation: kept
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_valid_cached_token_is_not_refreshed(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake)
    good = make_jwt(exp=time.time() + 3000)
    cache = _cache(tmp_path, id_token=good, expires_at=time.time() + 3000)
    with _client(ep, cache, interactive=False) as c:
        c.sites()
    assert ep.calls == []


def test_401_triggers_one_refresh_then_retry(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake)
    cache = _cache(
        tmp_path, id_token=make_jwt(exp=time.time() + 3000), expires_at=time.time() + 3000
    )
    first = {"done": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/sites" and not first["done"]:
            first["done"] = True
            return httpx.Response(401)
        return ep(req)

    with _client(handler, cache, interactive=False) as c:
        assert c.sites()
    assert len(ep.calls) == 1


def test_expired_refresh_non_interactive_raises(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake, refresh_ok=False)
    with (
        _client(ep, _cache(tmp_path), interactive=False) as c,
        pytest.raises(enca.AuthError, match="Not signed in") as e,
    ):
        c.sites()
    assert "refresh-1" not in str(e.value)


def test_expired_refresh_interactive_reprompts(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake, refresh_ok=False)
    cache = _cache(tmp_path)
    prompted = []

    def relogin() -> None:
        prompted.append(1)
        _auth.TokenCache(cache).save(
            _auth.TokenSet(make_jwt(exp=time.time() + 3600), "r3", time.time() + 3600)
        )

    http = httpx.Client(transport=httpx.MockTransport(ep))
    p = _auth.CachedLogin(http, cache=_auth.TokenCache(cache), interactive=True, login_fn=relogin)
    assert p.token()
    assert prompted == [1]


def test_no_cache_non_interactive(tmp_path: Path) -> None:
    with (
        enca.Client(
            cache_path=tmp_path / "none.json",
            interactive=False,
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        ) as c,
        pytest.raises(enca.AuthError, match="personal access token"),
    ):
        c.sites()


def test_login_exchanges_code_and_caches(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake)
    seen: dict[str, str] = {}

    def fake_print(msg: str) -> None:
        seen["url"] = msg.split()[-1]

    def fake_wait(port: int, timeout: float) -> dict[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(seen["url"]).query).items()}
        seen.update(q)
        return {"code": "abc", "state": q["state"]}

    cache = tmp_path / "t.json"
    email = _auth.login(
        "SignInWithApple",
        open_browser=False,
        cache_path=cache,
        http=httpx.Client(transport=httpx.MockTransport(ep)),
        _wait=fake_wait,
        _print=fake_print,
    )
    assert email == "owner@example.com"
    assert seen["identity_provider"] == "SignInWithApple"
    form = ep.calls[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "abc"
    assert _auth.pkce_challenge(form["code_verifier"]) == seen["code_challenge"]
    assert form["redirect_uri"] == "http://localhost:3000/"
    saved = _auth.TokenCache(cache).load()
    assert saved is not None and saved.refresh_token == "refresh-2"
    assert saved.refresh_expires_at is not None
    assert enca.logout(cache) is True and not cache.exists()
    assert enca.logout(cache) is False


def test_login_state_mismatch(tmp_path: Path) -> None:
    with pytest.raises(enca.AuthError, match="state"):
        _auth.login(
            open_browser=False,
            cache_path=tmp_path / "t.json",
            _wait=lambda p, t: {"code": "abc", "state": "forged"},
            _print=lambda m: None,
        )


def test_login_manual_paste(fake: FakeApi, tmp_path: Path) -> None:
    ep = TokenEndpoint(fake)
    url: dict[str, str] = {}

    def paste(prompt: str) -> str:
        state = parse_qs(urlparse(url["u"]).query)["state"][0]
        return f"http://localhost:3000/?code=xyz&state={state}"

    _auth.login(
        manual=True,
        cache_path=tmp_path / "t.json",
        http=httpx.Client(transport=httpx.MockTransport(ep)),
        _print=lambda m: url.setdefault("u", m.split()[-1]),
        _input=paste,
    )
    assert ep.calls[0]["code"] == "xyz"


def test_tokens_never_in_repr() -> None:
    t = _auth.TokenSet("secret-id", "secret-refresh", 1.0)
    assert "secret" not in repr(t)
    assert "secret" not in repr(_auth.StaticToken("secret-x"))
    assert "Ab3" not in repr(_auth.StaticToken("enca_pat_Ab3xyz"))


def test_env_token(monkeypatch: pytest.MonkeyPatch, fake: FakeApi) -> None:
    monkeypatch.setenv("ENCA_TOKEN", "enca_pat_" + "x" * 43)
    with enca.Client(transport=httpx.MockTransport(fake.handler)) as c:
        c.sites()
        assert c.max_concurrency == 2
    assert fake.requests[-1].headers["Authorization"] == "Bearer enca_pat_" + "x" * 43
