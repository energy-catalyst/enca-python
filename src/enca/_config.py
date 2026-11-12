"""Endpoints and on-disk locations. Everything here can be overridden."""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Cognito hosted sign-in domain.
AUTH_URL = "https://auth.connect.energycatalysttech.net"

#: The public Cognito app client (no secret) the web and mobile apps use.
CLIENT_ID = "14640g5kei8dff159vuj6uup3"

#: Redirect the hosted sign-in sends the browser back to. Stopgap: this is the
#: developer callback allowed on the app client. Personal access tokens will
#: replace the browser flow.
REDIRECT_URI = "http://localhost:3000/"
REDIRECT_PORT = 3000

SCOPES = "openid email profile"

#: Assumed production API host; override with ``ENCA_API_URL`` or
#: ``Client(api_url=...)``.
DEFAULT_API_URL = "https://api.energycatalysttech.net"


def api_url() -> str:
    return os.environ.get("ENCA_API_URL", DEFAULT_API_URL).rstrip("/")


def auth_url() -> str:
    return os.environ.get("ENCA_AUTH_URL", AUTH_URL).rstrip("/")


def config_dir() -> Path:
    """Where the token cache lives.

    ``$ENCA_CONFIG_DIR`` if set, else the platform's user config dir:
    ``~/.config/enca`` (Linux, honouring ``$XDG_CONFIG_HOME``),
    ``~/Library/Application Support/enca`` (macOS), ``%APPDATA%\\enca`` (Windows).
    """
    env = os.environ.get("ENCA_CONFIG_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "enca"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "enca"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "enca"


def token_cache_path() -> Path:
    return config_dir() / "tokens.json"
