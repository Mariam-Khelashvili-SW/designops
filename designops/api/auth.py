"""Single-admin login for the Design Ops UI.

Credentials live in env only (APP_ADMIN_USER / APP_ADMIN_PASSWORD). When the
password is unset, the gate is off so local tests and a first boot still work.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlparse

from starlette.requests import Request

from designops.core.config import get_settings

SESSION_USER_KEY = "auth_user"
SESSION_PW_KEY = "auth_pw"


def login_enabled() -> bool:
    return bool((get_settings().app_admin_password or "").strip())


def admin_username() -> str:
    return (get_settings().app_admin_user or "admin").strip() or "admin"


def session_secret() -> str:
    s = get_settings()
    if (s.app_session_secret or "").strip():
        return s.app_session_secret.strip()
    material = f"designops|{admin_username()}|{s.app_admin_password}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _password_stamp() -> str:
    pw = (get_settings().app_admin_password or "").encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), pw, hashlib.sha256).hexdigest()[:24]


def credentials_ok(username: str, password: str) -> bool:
    if not login_enabled():
        return False
    user_ok = hmac.compare_digest(username.strip(), admin_username())
    pw_ok = hmac.compare_digest(password, get_settings().app_admin_password)
    return bool(user_ok and pw_ok)


def is_authenticated(request: Request) -> bool:
    if not login_enabled():
        return True
    session = getattr(request, "session", None)
    if not isinstance(session, dict):
        return False
    return (
        session.get(SESSION_USER_KEY) == admin_username()
        and session.get(SESSION_PW_KEY) == _password_stamp()
    )


def mark_logged_in(request: Request) -> None:
    request.session[SESSION_USER_KEY] = admin_username()
    request.session[SESSION_PW_KEY] = _password_stamp()


def mark_logged_out(request: Request) -> None:
    request.session.clear()


def safe_next_url(raw: str | None, *, fallback: str = "/daily-report") -> str:
    """Only allow same-origin relative paths (no open redirects)."""
    value = (raw or "").strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    if value.startswith("/login"):
        return fallback
    return value


def is_public_path(path: str) -> bool:
    if path in {"/login", "/logout", "/health"}:
        return True
    return False
