"""Figma OAuth — authorization-code flow, raw over httpx.

OAuth *app* credentials (client id / secret / redirect) and the user grant
(refresh token) are both stored in Postgres ``app_state`` — nothing from ``.env``.

Connect → Figma consent → refresh token in Postgres.
OAuth tokens use ``Authorization: Bearer``; PAT uses ``X-Figma-Token`` (see figma.py).
"""

from __future__ import annotations

import base64
import time
from urllib.parse import urlencode

import httpx

from designops.core.config import Settings, get_settings

SCOPES = ["file_comments:read", "file_metadata:read", "current_user:read"]
_AUTH_URL = "https://www.figma.com/oauth"
_TOKEN_URL = "https://api.figma.com/v1/oauth/token"
_REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
_ME_URL = "https://api.figma.com/v1/me"

_STATE_KEY = "figma_oauth"  # user grant
_APP_STATE_KEY = "figma_oauth_app"  # client id / secret / redirect
STATE = "designops"
_DEFAULT_REDIRECT = "http://localhost:8077/oauth/figma/callback"


# --- OAuth app credentials (Config UI) ---------------------------------------


def _load_app() -> dict:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    try:
        with session_scope() as sess:
            row = sess.get(AppState, _APP_STATE_KEY)
            if row and row.value:
                return dict(row.value)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_app(data: dict) -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _APP_STATE_KEY)
        if row:
            row.value = data
        else:
            sess.add(AppState(key=_APP_STATE_KEY, value=data))


def get_oauth_app() -> dict:
    """Return {client_id, client_secret, redirect_uri} from Config / DB."""
    data = _load_app()
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    redirect_uri = (data.get("redirect_uri") or "").strip().rstrip("/")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri or _DEFAULT_REDIRECT,
    }


def oauth_app_configured() -> bool:
    app = get_oauth_app()
    return bool(app.get("client_id") and app.get("client_secret"))


def oauth_app_hint() -> dict:
    """Masked hints for the Config UI."""
    app = get_oauth_app()
    cid = app.get("client_id") or ""
    sec = app.get("client_secret") or ""

    def _mask(v: str) -> str | None:
        if not v:
            return None
        if len(v) <= 8:
            return "••••••••"
        return f"{v[:4]}…{v[-4:]}"

    return {
        "client_id_hint": _mask(cid),
        "client_secret_hint": _mask(sec),
        "redirect_uri": app.get("redirect_uri") or _DEFAULT_REDIRECT,
        "configured": bool(cid and sec),
    }


def save_oauth_app(client_id: str, client_secret: str, redirect_uri: str = "") -> None:
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    redir = (redirect_uri or "").strip().rstrip("/") or _DEFAULT_REDIRECT
    if not cid or not secret:
        raise RuntimeError("client id and client secret are required")
    _save_app(
        {
            "client_id": cid,
            "client_secret": secret,
            "redirect_uri": redir,
            "updated_at": time.time(),
        }
    )


def clear_oauth_app() -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _APP_STATE_KEY)
        if row:
            sess.delete(row)


def _basic_auth_header() -> str:
    app = get_oauth_app()
    raw = f"{app['client_id']}:{app['client_secret']}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


# --- User grant --------------------------------------------------------------


def _load(s: Settings | None = None) -> dict | None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    try:
        with session_scope() as sess:
            row = sess.get(AppState, _STATE_KEY)
            if row and row.value:
                return dict(row.value)
    except Exception:  # noqa: BLE001 — DB may be unavailable at import-time tooling
        pass
    return None


def _save(s: Settings | None, data: dict) -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _STATE_KEY)
        if row:
            row.value = data
        else:
            sess.add(AppState(key=_STATE_KEY, value=data))


def build_auth_url(state: str = STATE, settings: Settings | None = None) -> str:
    app = get_oauth_app()
    if not app.get("client_id") or not app.get("client_secret"):
        raise RuntimeError("Figma OAuth app not configured — save client id & secret on Config")
    q = {
        "client_id": app["client_id"],
        "redirect_uri": app["redirect_uri"],
        "scope": ",".join(SCOPES),
        "state": state or STATE,
        "response_type": "code",
    }
    return f"{_AUTH_URL}?{urlencode(q)}"


def exchange_code(code: str, settings: Settings | None = None) -> dict:
    """Swap the auth code for tokens (must complete within ~30s of consent)."""
    s = settings or get_settings()
    app = get_oauth_app()
    r = httpx.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "redirect_uri": app["redirect_uri"],
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    email = handle = None
    user_id = tok.get("user_id_string") or tok.get("user_id")
    try:
        me = httpx.get(
            _ME_URL,
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            timeout=15,
        )
        if me.is_success:
            body = me.json()
            email = body.get("email")
            handle = body.get("handle")
            user_id = body.get("id") or user_id
    except Exception:  # noqa: BLE001 — identity is a nicety
        pass
    data = {
        "refresh_token": tok.get("refresh_token"),
        "access_token": tok.get("access_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 7776000)),
        "email": email,
        "handle": handle,
        "user_id": str(user_id) if user_id is not None else None,
    }
    if not data["refresh_token"]:
        raise RuntimeError(
            "Figma did not return a refresh token — check the OAuth app "
            "is published (private is fine) and reconnect."
        )
    _save(s, data)
    return data


def is_connected(settings: Settings | None = None) -> bool:
    d = _load(settings or get_settings())
    return bool(d and d.get("refresh_token"))


def connected_label(settings: Settings | None = None) -> str | None:
    d = _load(settings or get_settings()) or {}
    return d.get("email") or d.get("handle")


def disconnect(settings: Settings | None = None) -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _STATE_KEY)
        if row:
            sess.delete(row)


def access_token(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    d = _load(s)
    if not d or not d.get("refresh_token"):
        raise RuntimeError("Figma account not connected")
    if d.get("access_token") and d.get("expires_at", 0) - 60 > time.time():
        return d["access_token"]
    if not oauth_app_configured():
        raise RuntimeError(
            "Figma OAuth app credentials missing — save client id & secret on Config to refresh"
        )
    r = httpx.post(
        _REFRESH_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"refresh_token": d["refresh_token"]},
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    d["access_token"] = tok["access_token"]
    d["expires_at"] = time.time() + int(tok.get("expires_in", 7776000))
    _save(s, d)
    return d["access_token"]
