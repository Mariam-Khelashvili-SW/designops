"""Google OAuth for Gmail — authorization-code flow, raw over httpx (no google SDK).

Two grants share the same OAuth client but different scopes + token stores:

- **Delivery** (`google_oauth`): `gmail.send` — email digests.
- **CRO mailbox** (`google_oauth_cro`): `gmail.readonly` — read cro@ only.

Flow: Connect → Google consent → refresh token in Postgres `app_state`. The app never
sees the user's Google password; the grant is revocable at myaccount.google.com.
"""

from __future__ import annotations

import base64
import json
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlencode

import httpx

from designops.core.config import Settings, get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
CRO_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_STATE_KEY = "google_oauth"
_CRO_STATE_KEY = "google_oauth_cro"
STATE_DELIVERY = "designops"
STATE_CRO = "cro"


def _path(s: Settings) -> Path:
    return Path(s.google_token_path)


def _load(s: Settings, *, key: str = _STATE_KEY) -> dict | None:
    """DB first (survives redeploys); file fallback only for delivery (dev/back-compat)."""
    from designops.core.db import session_scope
    from designops.core.models import AppState

    try:
        with session_scope() as sess:
            row = sess.get(AppState, key)
            if row and row.value:
                return dict(row.value)
    except Exception:  # noqa: BLE001 — DB may be unavailable at import-time tooling
        pass
    if key == _STATE_KEY:
        p = _path(s)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                return None
    return None


def _save(s: Settings, data: dict, *, key: str = _STATE_KEY) -> None:
    """Persist the token in Postgres so a container redeploy keeps the Gmail connection."""
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, key)
        if row:
            row.value = data
        else:
            sess.add(AppState(key=key, value=data))


def _build_auth_url(state: str, scopes: list[str], settings: Settings) -> str:
    q = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(q)}"


def build_auth_url(state: str = STATE_DELIVERY, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return _build_auth_url(state or STATE_DELIVERY, SCOPES, s)


def build_cro_auth_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return _build_auth_url(STATE_CRO, CRO_SCOPES, s)


def _exchange_code(
    code: str, *, settings: Settings, state_key: str
) -> dict:
    """Swap the auth code for tokens and persist the refresh token + connected email."""
    r = httpx.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    email = None
    try:
        u = httpx.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            timeout=15,
        )
        email = u.json().get("email")
    except Exception:  # noqa: BLE001 — email is a nicety, not required
        pass
    data = {
        "refresh_token": tok.get("refresh_token"),
        "access_token": tok.get("access_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "email": email,
    }
    if not data["refresh_token"]:
        raise RuntimeError(
            "Google did not return a refresh token — revoke the app at "
            "myaccount.google.com/permissions and reconnect."
        )
    _save(settings, data, key=state_key)
    return data


def exchange_code(code: str, settings: Settings | None = None) -> dict:
    return _exchange_code(code, settings=settings or get_settings(), state_key=_STATE_KEY)


def exchange_cro_code(code: str, settings: Settings | None = None) -> dict:
    return _exchange_code(
        code, settings=settings or get_settings(), state_key=_CRO_STATE_KEY
    )


def is_connected(settings: Settings | None = None) -> bool:
    d = _load(settings or get_settings())
    return bool(d and d.get("refresh_token"))


def is_cro_connected(settings: Settings | None = None) -> bool:
    d = _load(settings or get_settings(), key=_CRO_STATE_KEY)
    return bool(d and d.get("refresh_token"))


def connected_email(settings: Settings | None = None) -> str | None:
    return (_load(settings or get_settings()) or {}).get("email")


def connected_cro_email(settings: Settings | None = None) -> str | None:
    return (_load(settings or get_settings(), key=_CRO_STATE_KEY) or {}).get("email")


def disconnect(settings: Settings | None = None) -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _STATE_KEY)
        if row:
            sess.delete(row)
    p = _path(settings or get_settings())
    if p.exists():
        p.unlink()


def disconnect_cro(settings: Settings | None = None) -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _CRO_STATE_KEY)
        if row:
            sess.delete(row)


def _access_token(s: Settings, *, key: str = _STATE_KEY) -> str:
    d = _load(s, key=key)
    if not d or not d.get("refresh_token"):
        raise RuntimeError("Google account not connected")
    if d.get("access_token") and d.get("expires_at", 0) - 60 > time.time():
        return d["access_token"]
    r = httpx.post(
        _TOKEN_URL,
        data={
            "refresh_token": d["refresh_token"],
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    d["access_token"] = tok["access_token"]
    d["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    _save(s, d, key=key)
    return d["access_token"]


def cro_access_token(settings: Settings | None = None) -> str:
    return _access_token(settings or get_settings(), key=_CRO_STATE_KEY)


def send_gmail(
    recipients: list[str], subject: str, html: str, settings: Settings | None = None
) -> str:
    """Send an HTML email via the Gmail API as the connected user. Returns the message id."""
    s = settings or get_settings()
    access = _access_token(s)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = connected_email(s) or "me"
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = httpx.post(
        _SEND_URL,
        headers={"Authorization": f"Bearer {access}"},
        json={"raw": raw},
        timeout=30,
    )
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = r.text[:300]
        raise RuntimeError(f"{r.status_code} — {detail or r.text[:200]}")
    return r.json().get("id", "")
