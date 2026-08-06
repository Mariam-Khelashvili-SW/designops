"""HTTP client for transcript-processor public APIs."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from designops.core.config import Settings, get_settings

log = logging.getLogger(__name__)


class TranscriptApiError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _check_response(resp: httpx.Response, *, what: str) -> None:
    """Raise before JSON parse for auth / redirect / HTTP errors."""
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location") or "(no Location)"
        raise TranscriptApiError(
            f"{what}: HTTP {resp.status_code} redirect to {loc}. "
            "TRANSCRIPT_API_BASE_URL is hitting a login/session gate — "
            "use the transcript-processor origin that serves /api/transcripts "
            "(token auth, no browser session), and ensure that build bypasses "
            "session for /api/transcripts*."
        )
    if resp.status_code == 401:
        raise TranscriptApiError(
            f"{what}: Unauthorized — check TRANSCRIPT_API_TOKEN scope (TRANSCRIPTS or CALENDAR)"
        )
    if resp.status_code == 404:
        raise TranscriptApiError(f"{what}: not found (HTTP 404)")
    if resp.status_code >= 400:
        raise TranscriptApiError(f"{what}: HTTP {resp.status_code}: {resp.text[:300]}")


def _parse_json_response(resp: httpx.Response, *, what: str) -> Any:
    """Parse JSON with a clear error when the body is empty or HTML (common on prod misconfig)."""
    _check_response(resp, what=what)
    text = (resp.text or "").strip()
    if not text:
        raise TranscriptApiError(
            f"{what}: empty response body (HTTP {resp.status_code}). "
            "Check TRANSCRIPT_API_BASE_URL reaches transcript-processor."
        )
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and not text.startswith(("{", "[")):
        raise TranscriptApiError(
            f"{what}: non-JSON response (HTTP {resp.status_code}, "
            f"content-type={ctype or 'missing'}): {text[:180]!r}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise TranscriptApiError(
            f"{what}: invalid JSON (HTTP {resp.status_code}): {text[:180]!r}"
        ) from e


def list_transcripts(
    *,
    participant_emails: list[str] | None = None,
    include_content: bool = False,
    include_unscored: bool = True,
    exclude_internal: bool = True,
    name: str | None = None,
    limit: int = 100,
    offset: int = 0,
    settings: Settings | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """GET /api/transcripts — returns {items, counts, pagination, appliedFilters}."""
    s = settings or get_settings()
    if not s.transcript_api_configured:
        raise TranscriptApiError("TRANSCRIPT_API_BASE_URL / TRANSCRIPT_API_TOKEN not configured")

    params: dict[str, str] = {
        "limit": str(min(max(limit, 1), 500)),
        "offset": str(max(offset, 0)),
        "includeContent": "true" if include_content else "false",
        "includeUnscored": "true" if include_unscored else "false",
        "excludeInternal": "true" if exclude_internal else "false",
    }
    if participant_emails:
        params["participantEmails"] = ",".join(
            e.strip().lower() for e in participant_emails if e.strip()
        )
    if name and name.strip():
        params["name"] = name.strip()

    url = f"{s.transcript_api_base_url.rstrip('/')}/api/transcripts?{urlencode(params)}"
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            resp = client.get(url, headers=_headers(s.transcript_api_token))
    except httpx.HTTPError as e:
        raise TranscriptApiError(f"transcripts API request failed: {e}") from e
    data = _parse_json_response(resp, what="transcripts list")
    if not isinstance(data, dict) or "items" not in data:
        raise TranscriptApiError("Unexpected transcripts API response shape")
    return data


def get_transcript(
    transcript_id: str,
    *,
    settings: Settings | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """GET /api/transcripts/:id — returns the transcript item (with content)."""
    s = settings or get_settings()
    if not s.transcript_api_configured:
        raise TranscriptApiError("TRANSCRIPT_API_BASE_URL / TRANSCRIPT_API_TOKEN not configured")
    tid = (transcript_id or "").strip()
    if not tid:
        raise TranscriptApiError("Missing transcript id")

    url = f"{s.transcript_api_base_url.rstrip('/')}/api/transcripts/{tid}"
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            resp = client.get(url, headers=_headers(s.transcript_api_token))
    except httpx.HTTPError as e:
        raise TranscriptApiError(f"get transcript request failed: {e}") from e
    data = _parse_json_response(resp, what=f"get transcript {tid}")
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise TranscriptApiError("Unexpected get-transcript response shape")
    return item
