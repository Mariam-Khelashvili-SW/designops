"""Client call dates for weekly health.

Primary: Transcript calendar-meetings API (past + upcoming).
Fallback for last call: Fairwind cached transcripts (event_start_time + attendees).
"""

from __future__ import annotations

import glob
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from designops.core.config import Settings, get_settings

log = logging.getLogger(__name__)

DEFAULT_OLGA_EMAIL = "olga@scandiweb.com"


def empty_call_dates() -> dict:
    return {
        "last_call_date": None,
        "last_call_display": "n/a",
        "last_call_title": None,
        "next_call_date": None,
        "next_call_display": "n/a",
        "next_call_title": None,
        "calls_muted": True,
    }


def _fmt_call_date(d: date | None) -> str:
    if d is None:
        return "n/a"
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _parse_start(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def design_participant_emails(
    roster_emails: list[str],
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Design-team emails + Olga (always included for client calls)."""
    s = settings or get_settings()
    olga = (s.olga_email or DEFAULT_OLGA_EMAIL).strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for e in list(roster_emails) + [olga]:
        email = (e or "").strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def normalize_email_domains(domains: list[str] | None) -> list[str]:
    """Dedupe domains; drop empties. Keep apex + subdomains as stored on Account."""
    out: list[str] = []
    seen: set[str] = set()
    for d in domains or []:
        dom = (d or "").strip().lower().lstrip("@")
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append(dom)
    return out


def fetch_calendar_meetings(
    *,
    email_domains: list[str],
    participant_emails: list[str],
    when: str = "all",
    limit: int = 100,
    settings: Settings | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Call Transcript /api/calendar-meetings. Returns raw JSON payload."""
    s = settings or get_settings()
    if not s.transcript_api_configured:
        raise RuntimeError(
            "Transcript API not configured — set TRANSCRIPT_API_BASE_URL and TRANSCRIPT_API_TOKEN."
        )
    domains = normalize_email_domains(email_domains)
    participants = [e.strip().lower() for e in participant_emails if e and "@" in e]
    if not domains:
        return {"items": [], "total": 0, "filters": {"emailDomains": []}}
    if not participants:
        raise RuntimeError("participantEmails required for calendar-meetings")

    params = {
        "emailDomains": ",".join(domains),
        "participantEmails": ",".join(participants),
        "when": when,
        "token": s.transcript_api_token,
        "limit": str(limit),
    }
    url = f"{s.transcript_api_base_url.rstrip('/')}/api/calendar-meetings?{urlencode(params)}"
    with httpx.Client(timeout=timeout_s) as client:
        r = client.get(url)
        # 404 = domain/account not known to Transcript — treat as no meetings.
        if r.status_code == 404:
            return {
                "items": [],
                "total": 0,
                "truncated": False,
                "filters": {"emailDomains": domains},
                "error": "not_found",
            }
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("calendar-meetings returned non-object JSON")
    return data


def derive_call_dates_from_meetings(
    items: list[dict],
    *,
    as_of: date,
) -> dict:
    """Pick last (past) and next (upcoming) meetings relative to as_of."""
    past: list[tuple[date, str]] = []
    future: list[tuple[date, str]] = []
    as_of_end = datetime(
        as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc
    )

    for it in items or []:
        status = (it.get("status") or "").strip().lower()
        if status in {"cancelled", "canceled"}:
            continue
        start = _parse_start(it.get("startTime"))
        if start is None:
            continue
        day = start.date()
        title = (it.get("summary") or "").strip() or "Client call"
        upcoming_flag = it.get("isUpcoming")
        if upcoming_flag is True or (upcoming_flag is None and start > as_of_end):
            future.append((day, title))
        elif upcoming_flag is False or start <= as_of_end:
            past.append((day, title))
        else:
            future.append((day, title))

    last_call = last_title = None
    if past:
        last_call, last_title = max(past, key=lambda x: x[0])

    next_call = next_title = None
    if future:
        # Only dates strictly after as_of for "next"
        upcoming = [(d, t) for d, t in future if d > as_of]
        if upcoming:
            next_call, next_title = min(upcoming, key=lambda x: x[0])

    return {
        "last_call_date": last_call.isoformat() if last_call else None,
        "last_call_display": _fmt_call_date(last_call),
        "last_call_title": last_title,
        "next_call_date": next_call.isoformat() if next_call else None,
        "next_call_display": _fmt_call_date(next_call),
        "next_call_title": next_title,
        "calls_muted": last_call is None and next_call is None,
    }


def _fairwind_transcript_items(
    corpus_dir: str,
    account_id: str,
    participant_emails: list[str],
) -> list[dict]:
    """Read cached Fairwind transcript JSONs, filter by participant, return items
    compatible with ``derive_call_dates_from_meetings``.
    """
    pattern = str(
        Path(corpus_dir) / account_id / "health_meetings_*" / "files" / "json" / "transcripts" / "**" / "*.json"
    )
    files = glob.glob(pattern, recursive=True)
    if not files:
        return []

    participant_set = {e.strip().lower() for e in participant_emails if e and "@" in e}
    seen_ids: set[int] = set()
    items: list[dict] = []

    for fp in files:
        try:
            tr = json.loads(Path(fp).read_text())
        except (json.JSONDecodeError, OSError):
            continue

        tr_id = tr.get("id")
        if tr_id in seen_ids:
            continue
        seen_ids.add(tr_id)

        attendee_emails = {
            (a.get("email") or "").strip().lower()
            for a in (tr.get("attendees") or [])
        }
        if not attendee_emails & participant_set:
            continue

        start = tr.get("event_start_time")
        if not start:
            continue

        items.append({
            "startTime": start,
            "summary": tr.get("title") or "Client call",
            "status": tr.get("status") or "completed",
            "isUpcoming": False,
        })

    return items


def call_dates_for_domains(
    email_domains: list[str],
    participant_emails: list[str],
    *,
    as_of: date,
    settings: Settings | None = None,
    fairwind_account_id: str | None = None,
) -> tuple[dict, dict]:
    """Fetch meetings and derive call-date card fields.

    Primary source: Transcript calendar-meetings API.
    Fallback for last call: Fairwind cached transcripts (when API has no past meetings).

    Returns (fields, meta) where meta has source/counts for coverage.
    """
    s = settings or get_settings()
    domains = normalize_email_domains(email_domains)

    fields = empty_call_dates()
    meta: dict = {}
    transcript_api_used = False

    # --- Primary: Transcript calendar API ---
    if domains and s.transcript_api_configured:
        data = fetch_calendar_meetings(
            email_domains=domains,
            participant_emails=participant_emails,
            when="all",
            settings=s,
        )
        items = list(data.get("items") or [])
        fields = derive_call_dates_from_meetings(items, as_of=as_of)
        transcript_api_used = True
        meta = {
            "source": "transcript-calendar",
            "domains": domains,
            "total": data.get("total"),
            "returned": len(items),
            "truncated": bool(data.get("truncated")),
            "last_call": fields.get("last_call_date"),
            "next_call": fields.get("next_call_date"),
        }

    # --- Fallback: Fairwind cached transcripts for last call ---
    fw_account = (fairwind_account_id or "").strip()
    if not fields.get("last_call_date") and fw_account:
        corpus_dir = s.corpus_store_dir
        fw_items = _fairwind_transcript_items(corpus_dir, fw_account, participant_emails)
        if fw_items:
            fw_fields = derive_call_dates_from_meetings(fw_items, as_of=as_of)
            if fw_fields.get("last_call_date"):
                fields["last_call_date"] = fw_fields["last_call_date"]
                fields["last_call_display"] = fw_fields["last_call_display"]
                fields["last_call_title"] = fw_fields["last_call_title"]
                fields["calls_muted"] = fields["last_call_date"] is None and fields["next_call_date"] is None
                meta["fw_fallback"] = True
                meta["fw_transcripts"] = len(fw_items)
                meta["last_call"] = fw_fields["last_call_date"]
                log.info("Fairwind fallback: last call %s from %d transcripts", fw_fields["last_call_date"], len(fw_items))

    if not meta:
        if not domains:
            meta = {"source": "skipped", "reason": "no_email_domains"}
        else:
            meta = {"source": "skipped", "reason": "transcript_api_not_configured"}

    return fields, meta
