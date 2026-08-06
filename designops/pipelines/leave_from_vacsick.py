"""Detect designer leave from VACSICK Tempo worklogs (≥8h/day → on_leave).

Persists Person.status / leave_until so weekly availability_marker treats them
as OUT (or PARTIAL if leave ends mid-week). Does not clear manual leave when
Tempo has no hits. Skips status=out people.

Tempo Cloud omits issue keys; we resolve issue ids via Jira and keep VACSICK
rows (description keywords are a fallback when Jira is unavailable).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from designops.adapters.tempo import VACSICK_PROJECT_KEY, TempoClient
from designops.core.config import Settings, get_settings
from designops.core.enums import PersonStatus
from designops.core.models import Person

# One full day logged to VACSICK ⇒ that calendar day is leave.
LEAVE_DAY_HOURS_THRESHOLD = 8.0

# Tempo fetch window: report week plus ~4 weeks so multi-week vacations extend leave_until.
LEAVE_SYNC_HORIZON_DAYS = 28

# Fallback when Jira cannot resolve Tempo issue ids.
_LEAVE_DESC_MARKERS = (
    "vacation",
    "sick",
    "unpaid",
    "day-off",
    "day off",
    "dayoff",
    "vacsick",
    "leave",
)


@dataclass(frozen=True, slots=True)
class LeaveDetection:
    person_id: str
    full_name: str
    leave_days: tuple[date, ...]
    leave_from: date
    leave_until: date
    total_hours: float
    updated: bool


def hours_by_day(
    worklogs: Iterable[dict],
    *,
    account_id: str,
    from_date: date,
    to_date: date,
) -> dict[date, float]:
    """Sum hours per calendar day for one worker in [from_date, to_date]."""
    totals: dict[date, float] = defaultdict(float)
    aid = (account_id or "").strip()
    for w in worklogs:
        if (w.get("account_id") or "").strip() != aid:
            continue
        started = w.get("started")
        if not isinstance(started, date):
            continue
        if started < from_date or started > to_date:
            continue
        totals[started] += float(w.get("hours") or 0)
    return {d: round(h, 2) for d, h in sorted(totals.items())}


def leave_days_from_hours(
    day_hours: dict[date, float],
    *,
    threshold: float = LEAVE_DAY_HOURS_THRESHOLD,
) -> list[date]:
    """Days where logged VACSICK hours meet/exceed the leave threshold."""
    return [d for d, h in sorted(day_hours.items()) if h >= threshold]


def _gap_is_nonworking_only(prev_end: date, next_start: date) -> bool:
    """True when every calendar day strictly between the two dates is Sat/Sun.

    Tempo does not log VACSICK on weekends, so Fri→Mon leave is one vacation.
    A weekday in the gap (e.g. Mon sick + next Fri sick) stays two blocks.
    """
    d = prev_end + timedelta(days=1)
    if d >= next_start:
        return True  # adjacent or overlapping
    while d < next_start:
        if d.weekday() < 5:  # Mon–Fri
            return False
        d += timedelta(days=1)
    return True


def contiguous_leave_blocks(days: list[date]) -> list[tuple[date, date]]:
    """Group leave days into ranges; weekend-only gaps do not split a vacation."""
    if not days:
        return []
    sorted_days = sorted(set(days))
    blocks: list[tuple[date, date]] = []
    start = end = sorted_days[0]
    for d in sorted_days[1:]:
        if (d - end).days == 1 or _gap_is_nonworking_only(end, d):
            end = d
        else:
            blocks.append((start, end))
            start = end = d
    blocks.append((start, end))
    return blocks


def pick_leave_window(
    blocks: list[tuple[date, date]],
    reference_date: date,
) -> tuple[tuple[date, date] | None, bool]:
    """Choose leave_from/until for the roster relative to a report/sync day.

    Returns (window, on_leave_today). When the reference day falls inside a block,
    that block is used and on_leave_today is True. Otherwise the next future block
    is stored (status stays on_leave with a future leave_from for upcoming notes).
    Past-only blocks clear the window (None).
    """
    if not blocks:
        return None, False
    for start, end in blocks:
        if start <= reference_date <= end:
            return (start, end), True
    future = next((b for b in blocks if b[0] > reference_date), None)
    if future:
        return future, False
    return None, False


def is_leave_description(description: str | None) -> bool:
    text = (description or "").strip().lower()
    if not text:
        return False
    return any(m in text for m in _LEAVE_DESC_MARKERS)


def apply_leave_from_days(
    person: Person,
    leave_days: list[date],
    *,
    reference_date: date | None = None,
) -> bool:
    """Apply VACSICK leave using contiguous blocks (weekend gaps allowed).

    Never touches status=out. Never clears leave when leave_days is empty.
    Sets leave_from/leave_until to the block containing reference_date, or the
    next upcoming block when between/absent — never bridges a weekday gap
    (e.g. Mon sick + next Fri sick must not imply one continuous vacation).
    """
    if not leave_days:
        return False
    if person.status == PersonStatus.OUT:
        return False

    ref = reference_date or date.today()
    window, _on_leave_today = pick_leave_window(
        contiguous_leave_blocks(leave_days), ref
    )
    changed = False
    if window is None:
        if person.status == PersonStatus.ON_LEAVE:
            person.status = PersonStatus.ACTIVE
            changed = True
        if person.leave_from is not None:
            person.leave_from = None
            changed = True
        if person.leave_until is not None:
            person.leave_until = None
            changed = True
        return changed

    start, until = window
    if person.status != PersonStatus.ON_LEAVE:
        person.status = PersonStatus.ON_LEAVE
        changed = True
    if person.leave_from != start:
        person.leave_from = start
        changed = True
    if person.leave_until != until:
        person.leave_until = until
        changed = True
    return changed


def resolve_issue_projects(
    issue_ids: Iterable[str],
    *,
    settings: Settings | None = None,
) -> dict[str, dict]:
    """Map Tempo/Jira issue id → {key, project_key, summary} via Jira."""
    from designops.adapters.jira import JiraClient

    s = settings or get_settings()
    if not s.jira_configured:
        return {}
    ids = sorted({str(i) for i in issue_ids if i})
    if not ids:
        return {}
    client = JiraClient(s)
    out: dict[str, dict] = {}
    with client._client() as http:
        for iid in ids:
            r = http.get(
                f"/rest/api/3/issue/{iid}",
                params={"fields": "summary,project,issuetype"},
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json() or {}
            fields = data.get("fields") or {}
            proj = (fields.get("project") or {}).get("key") or ""
            out[str(iid)] = {
                "key": (data.get("key") or "").upper() or None,
                "project_key": str(proj).upper() if proj else None,
                "summary": fields.get("summary"),
            }
    return out


def filter_vacsick_worklogs(
    worklogs: list[dict],
    *,
    issue_meta: dict[str, dict] | None = None,
    project_key: str = VACSICK_PROJECT_KEY,
) -> list[dict]:
    """Keep worklogs on VACSICK (via resolved project) or leave-like description."""
    pk = project_key.upper()
    meta = issue_meta or {}
    kept: list[dict] = []
    for w in worklogs:
        iid = w.get("issue_id")
        info = meta.get(str(iid)) if iid else None
        if info and (info.get("project_key") or "").upper() == pk:
            enriched = dict(w)
            if info.get("key"):
                enriched["issue_key"] = info["key"]
            enriched["project_key"] = pk
            kept.append(enriched)
            continue
        key = (w.get("issue_key") or "").upper()
        if key.startswith(f"{pk}-"):
            kept.append(w)
            continue
        if is_leave_description(w.get("description")):
            kept.append(w)
    return kept


def leave_sync_until(week_monday: date, week_friday: date | None = None) -> date:
    """Last calendar day to scan in Tempo for VACSICK (multi-week vacations)."""
    fri = week_friday or (week_monday + timedelta(days=4))
    horizon_end = week_monday + timedelta(days=LEAVE_SYNC_HORIZON_DAYS - 1)
    return max(fri, horizon_end)


def detect_leave_from_worklogs(
    people: list[Person],
    worklogs: list[dict],
    *,
    week_monday: date,
    week_friday: date,
    reference_date: date | None = None,
    threshold: float = LEAVE_DAY_HOURS_THRESHOLD,
) -> list[LeaveDetection]:
    """Pure detection + in-memory Person updates from normalized worklogs."""
    ref = reference_date or week_monday
    results: list[LeaveDetection] = []
    for person in people:
        aid = (person.jira_account_id or "").strip()
        if not aid or person.status == PersonStatus.OUT:
            continue
        by_day = hours_by_day(
            worklogs, account_id=aid, from_date=week_monday, to_date=week_friday
        )
        days = leave_days_from_hours(by_day, threshold=threshold)
        if not days:
            continue
        updated = apply_leave_from_days(person, days, reference_date=ref)
        window, _ = pick_leave_window(contiguous_leave_blocks(days), ref)
        leave_from, leave_until = window if window else (min(days), max(days))
        results.append(
            LeaveDetection(
                person_id=str(person.id),
                full_name=person.full_name,
                leave_days=tuple(days),
                leave_from=leave_from,
                leave_until=leave_until,
                total_hours=round(sum(by_day[d] for d in days), 2),
                updated=updated,
            )
        )
    return results


def sync_leave_from_vacsick(
    people: list[Person],
    *,
    week_monday: date,
    week_friday: date | None = None,
    reference_date: date | None = None,
    settings: Settings | None = None,
    client: TempoClient | None = None,
) -> dict:
    """Fetch Tempo worklogs, keep VACSICK/leave rows, persist on_leave.

    Returns a coverage-shaped dict:
      configured, fetched, detections[...], updated_names, note
    """
    s = settings or get_settings()
    fri = week_friday or (week_monday + timedelta(days=4))
    sync_to = leave_sync_until(week_monday, fri)
    if not s.tempo_configured:
        return {
            "configured": False,
            "fetched": 0,
            "detections": [],
            "updated_names": [],
            "note": "TEMPO_API_TOKEN not set — skip VACSICK leave sync",
        }

    tempo = client or TempoClient(s)
    account_ids = [
        (p.jira_account_id or "").strip()
        for p in people
        if p.jira_account_id and p.status != PersonStatus.OUT
    ]
    # Do not pass project= — Tempo Cloud returns 400 for project=VACSICK.
    all_logs = tempo.list_worklogs(
        from_date=week_monday,
        to_date=sync_to,
        project=None,
        account_ids=account_ids or None,
    )
    issue_ids = [w.get("issue_id") for w in all_logs if w.get("issue_id")]
    issue_meta = resolve_issue_projects(issue_ids, settings=s)
    worklogs = filter_vacsick_worklogs(all_logs, issue_meta=issue_meta)
    detections = detect_leave_from_worklogs(
        people,
        worklogs,
        week_monday=week_monday,
        week_friday=sync_to,
        reference_date=reference_date or week_monday,
    )
    return {
        "configured": True,
        "fetched": len(worklogs),
        "fetched_all": len(all_logs),
        "sync_until": sync_to.isoformat(),
        "detections": [
            {
                "name": d.full_name,
                "person_id": d.person_id,
                "leave_from": d.leave_from.isoformat(),
                "leave_until": d.leave_until.isoformat(),
                "days": [x.isoformat() for x in d.leave_days],
                "hours": d.total_hours,
                "updated": d.updated,
            }
            for d in detections
        ],
        "updated_names": [d.full_name for d in detections if d.updated],
        "note": None,
    }
