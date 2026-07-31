"""Gmail adapter — CRO mailbox read + Fairwind-fallback stub.

CRO: separate OAuth grant (`gmail.readonly`) lists only mail for CRO_MAILBOX_EMAIL
(to / cc / deliveredto). Config preview + daily-digest beyond_daily ingest.
"""

from __future__ import annotations

import base64
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from designops.adapters import google_oauth
from designops.adapters.documents import Document
from designops.core.config import Settings, get_settings

_GMAIL_LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_GMAIL_GET = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"
_FROM_EMAIL = re.compile(r"<([^>]+)>")


def cro_mailbox_query(mailbox: str, *, after: date | None = None, before: date | None = None) -> str:
    """Gmail search query constrained to the CRO address. Never returns an unconstrained inbox query."""
    addr = (mailbox or "").strip().lower()
    if not addr or "@" not in addr:
        raise ValueError("CRO mailbox email is required")
    parts = [
        f"(to:{addr} OR cc:{addr} OR deliveredto:{addr})",
    ]
    if after:
        parts.append(f"after:{after.isoformat()}")
    if before:
        # Gmail `before:` is exclusive on the calendar date.
        parts.append(f"before:{before.isoformat()}")
    return " ".join(parts)


def list_messages(query: str, after: date, before: date) -> list[Document]:
    raise NotImplementedError(
        "Generic Gmail source is dormant in v1 (source_mode=fairwind). "
        "Use list_cro_messages / list_cro_documents for the CRO mailbox."
    )


def list_cro_messages(
    *,
    after: date | None = None,
    before: date | None = None,
    max_results: int = 20,
    settings: Settings | None = None,
) -> list[dict]:
    """List recent messages for CRO_MAILBOX_EMAIL (metadata + snippet) for Config preview."""
    msgs = _fetch_cro_raw(
        after=after,
        before=before,
        max_results=max_results,
        fmt="metadata",
        settings=settings,
    )
    out: list[dict] = []
    for msg in msgs:
        parsed = _message_preview(msg, body="")
        if parsed:
            out.append(parsed)
    return out


def list_cro_documents(
    *,
    after: date,
    before: date,
    max_results: int = 40,
    settings: Settings | None = None,
) -> list[Document]:
    """CRO mail for daily digest — beyond_daily signal (folder=cro), report-day only upstream."""
    s = settings or get_settings()
    mailbox = (s.cro_mailbox_email or "").strip()
    msgs = _fetch_cro_raw(
        after=after,
        before=before,
        max_results=max_results,
        fmt="full",
        settings=s,
    )
    docs: list[Document] = []
    for msg in msgs:
        body = _extract_body_text(msg.get("payload") or {}) or (msg.get("snippet") or "")
        parsed = _message_preview(msg, body=body)
        if not parsed:
            continue
        event = parsed.get("date")
        if isinstance(event, datetime):
            event_d = event.date()
            sent_at = event
        elif isinstance(event, date):
            event_d = event
            sent_at = None
        else:
            event_d = after
            sent_at = None
        author = _email_from_header(parsed.get("from") or "")
        docs.append(
            Document(
                source="gmail",
                external_id=parsed["id"],
                event_date=event_d,
                author_identity=author,
                title=parsed.get("subject") or "(no subject)",
                body=(body or parsed.get("snippet") or "")[:8000],
                message_id=parsed["id"],
                sent_at=sent_at,
                url=None,
                project_hint=None,
                raw={
                    "folder": "cro",
                    "mailbox": mailbox,
                    "from": parsed.get("from"),
                    "to": parsed.get("to"),
                    "cc": parsed.get("cc"),
                    "subject": parsed.get("subject"),
                },
            )
        )
    return docs


def _fetch_cro_raw(
    *,
    after: date | None,
    before: date | None,
    max_results: int,
    fmt: str,
    settings: Settings | None,
) -> list[dict]:
    s = settings or get_settings()
    mailbox = (s.cro_mailbox_email or "").strip()
    if not mailbox:
        raise ValueError("CRO_MAILBOX_EMAIL is not set")
    if not google_oauth.is_cro_connected(s):
        raise RuntimeError("CRO mailbox Google account not connected")

    q = cro_mailbox_query(mailbox, after=after, before=before)
    access = google_oauth.cro_access_token(s)
    headers = {"Authorization": f"Bearer {access}"}
    params_get: dict = {"format": fmt}
    if fmt == "metadata":
        params_get["metadataHeaders"] = ["From", "To", "Subject", "Date", "Cc"]

    with httpx.Client(timeout=60.0) as client:
        r = client.get(
            _GMAIL_LIST,
            headers=headers,
            params={"q": q, "maxResults": max(1, min(int(max_results), 50))},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Gmail list failed: {_gmail_err(r)}")
        ids = [m["id"] for m in (r.json().get("messages") or []) if m.get("id")]
        seen: set[str] = set()
        out: list[dict] = []
        for mid in ids:
            if mid in seen:
                continue
            seen.add(mid)
            gr = client.get(_GMAIL_GET.format(id=mid), headers=headers, params=params_get)
            if gr.status_code >= 400:
                continue
            out.append(gr.json())
        return out


def _gmail_err(r: httpx.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", "") or r.text[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]


def _header_map(payload: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").strip().lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, IndexError):
        return None


def _email_from_header(from_hdr: str) -> str:
    m = _FROM_EMAIL.search(from_hdr or "")
    if m:
        return m.group(1).strip().lower()
    return (from_hdr or "").strip().lower()


def _message_preview(msg: dict, *, body: str = "") -> dict | None:
    mid = msg.get("id")
    if not mid:
        return None
    payload = msg.get("payload") or {}
    headers = _header_map(payload)
    dt = _parse_date(headers.get("date"))
    if dt is None and msg.get("internalDate"):
        try:
            ms = int(msg["internalDate"])
            dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            dt = None
    return {
        "id": mid,
        "thread_id": msg.get("threadId"),
        "date": dt,
        "from": headers.get("from") or "",
        "to": headers.get("to") or "",
        "cc": headers.get("cc") or "",
        "subject": headers.get("subject") or "(no subject)",
        "snippet": (msg.get("snippet") or "").strip(),
        "body": body,
    }


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripping a simple text/html part."""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data")
    if data and mime == "text/plain":
        return _decode_body_data(data)
    if data and mime == "text/html":
        return re.sub(r"<[^>]+>", " ", _decode_body_data(data))
    plain = html = ""
    for part in payload.get("parts") or []:
        pm = (part.get("mimeType") or "").lower()
        if pm.startswith("multipart/"):
            nested = _extract_body_text(part)
            if nested and not plain:
                plain = nested
            continue
        pdata = (part.get("body") or {}).get("data")
        if not pdata:
            continue
        text = _decode_body_data(pdata)
        if pm == "text/plain" and not plain:
            plain = text
        elif pm == "text/html" and not html:
            html = re.sub(r"<[^>]+>", " ", text)
    return (plain or html or "").strip()
