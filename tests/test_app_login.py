"""Unit tests for the single-admin login gate (no running server)."""

from designops.api.auth import credentials_ok, login_enabled, safe_next_url
from designops.core.config import get_settings


def test_safe_next_url_rejects_open_redirects():
    assert safe_next_url("/weekly-health") == "/weekly-health"
    assert safe_next_url("/runs/abc?sent=ok") == "/runs/abc?sent=ok"
    assert safe_next_url("https://evil.example/phish") == "/"
    assert safe_next_url("//evil.example") == "/"
    assert safe_next_url("/login") == "/"
    assert safe_next_url("") == "/"


def test_credentials_require_configured_password(monkeypatch):
    monkeypatch.setenv("APP_ADMIN_USER", "admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "")
    get_settings.cache_clear()
    assert login_enabled() is False
    assert credentials_ok("admin", "x") is False
    get_settings.cache_clear()


def test_credentials_ok_with_password(monkeypatch):
    monkeypatch.setenv("APP_ADMIN_USER", "admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    try:
        assert login_enabled() is True
        assert credentials_ok("admin", "s3cret-pass") is True
        assert credentials_ok("admin", "wrong") is False
        assert credentials_ok("nope", "s3cret-pass") is False
    finally:
        get_settings.cache_clear()
