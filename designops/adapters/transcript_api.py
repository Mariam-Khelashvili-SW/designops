"""HTTP client for transcript-processor public APIs."""

from __future__ import annotations

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
        params["participantEmails"] = ",".join(e.strip().lower() for e in participant_emails if e.strip())
    if name and name.strip():
        params["name"] = name.strip()

    url = f"{s.transcript_api_base_url.rstrip('/')}/api/transcripts?{urlencode(params)}"
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(url, headers=_headers(s.transcript_api_token))
    if resp.status_code == 401:
        raise TranscriptApiError("Unauthorized — check TRANSCRIPT_API_TOKEN scope (TRANSCRIPTS)")
    if resp.status_code >= 400:
        raise TranscriptApiError(f"transcripts API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
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
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(url, headers=_headers(s.transcript_api_token))
    if resp.status_code == 404:
        raise TranscriptApiError(f"Transcript not found: {tid}")
    if resp.status_code == 401:
        raise TranscriptApiError("Unauthorized — check TRANSCRIPT_API_TOKEN scope (TRANSCRIPTS)")
    if resp.status_code >= 400:
        raise TranscriptApiError(f"transcripts API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise TranscriptApiError("Unexpected get-transcript response shape")
    return item
