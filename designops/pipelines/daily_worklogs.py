"""Attach Jira tickets the designer logged time against on the report day.

Tickets come from the Atlassian connector (worklogDate + worklogAuthor), not a
windowed Fairwind export. Time-log buckets, hardware, epics, and obvious
dev/QA types are dropped. If Jira is unreachable the digest stays report-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from designops.adapters.documents import Document
from designops.adapters.jira import (
    JiraClient,
    status_unchanged_days,
)
from designops.adapters.tempo import VACSICK_PROJECT_KEY, TempoClient
from designops.core.config import Settings, get_settings
from designops.core.identity import effective_status
from designops.core.registry import ProjectRegistry
from designops.pipelines.weekly_availability import (
    extract_ticket_keys,
    is_excluded_ticket,
)
from designops.pipelines.weekly_health_math import (
    DESIGN_COMPONENTS,
    is_timelog_bucket,
)

STALE_STATUS_DAYS = 3
LOOKBACK_DAYS = 7

# Implementation tickets a designer may log against that still aren't design-owned.
_NON_DESIGN_ISSUE_TYPES = frozenset(
    {
        "development",
        "dev task",
        "qa",
        "qa task",
        "quality",
        "quality assurance",
        "test",
        "testing",
        "sub-task",
        "subtask",
    }
)


@dataclass
class TicketRow:
    key: str
    summary: str
    status: str
    hours: float | None  # None → em dash (named in report, no worklog)
    url: str | None
    stale_days: int | None
    project_key: str | None
    project_name: str | None
    original_hours: float | None = None
    spent_hours: float | None = None
    issue_type: str | None = None
    components: list[str] = field(default_factory=list)
    named_only: bool = False  # report named it; no worklog today


@dataclass
class WorklogBundle:
    available: bool
    note: str | None = None
    tickets: list[tuple[str, TicketRow]] = field(default_factory=list)  # (person, row)
    hours_by_person: dict[str, float] = field(default_factory=dict)
    hours_by_person_project: dict[tuple[str, str], float] = field(default_factory=dict)
    zero_ranges: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, note: str) -> WorklogBundle:
        return cls(available=False, note=note)


def format_hours(hours: float | None) -> str:
    if hours is None:
        return "—"
    return f"{hours:.1f}h"


def _has_design_component(components: list[str] | None) -> bool:
    for c in components or []:
        if str(c).strip().lower() in DESIGN_COMPONENTS:
            return True
    return False


def is_design_owned_ticket(ticket: dict) -> bool:
    """Drop buckets / hardware / epics / obvious dev-QA; keep designer worklogs."""
    if is_excluded_ticket(ticket) or is_timelog_bucket(ticket):
        return False
    itype = (ticket.get("issue_type") or "").strip().lower()
    if itype in _NON_DESIGN_ISSUE_TYPES and not _has_design_component(
        ticket.get("components")
    ):
        return False
    return True


def _ticket_from_doc(
    doc: Document,
    *,
    hours: float | None,
    stale_days: int | None,
    named_only: bool = False,
) -> TicketRow:
    raw = doc.raw or {}
    key = (raw.get("key") or doc.external_id or "").upper()
    return TicketRow(
        key=key,
        summary=(raw.get("summary") or doc.title or key).replace(f"{key}: ", "", 1),
        status=raw.get("status") or "",
        hours=hours,
        url=doc.url,
        stale_days=stale_days if stale_days is not None and stale_days >= STALE_STATUS_DAYS else None,
        project_key=(raw.get("project_key") or doc.project_hint or "").upper() or None,
        project_name=raw.get("project_name"),
        original_hours=raw.get("original_hours"),
        spent_hours=raw.get("spent_hours"),
        issue_type=raw.get("issue_type") or doc.jira_issue_type,
        components=list(raw.get("components") or []),
        named_only=named_only,
    )


def _canonical_project(registry: ProjectRegistry | None, row: TicketRow, fallback: str) -> str:
    if registry is not None:
        if row.project_key:
            entry = registry.resolve_jira_key(row.project_key)
            if entry:
                return entry.canonical_name
        if row.project_name:
            entry = registry.resolve(row.project_name)
            if entry:
                return entry.canonical_name
    return (row.project_name or row.project_key or fallback).strip() or fallback


def collect_daily_worklogs(
    roster_rows: list,
    report_date: date,
    *,
    settings: Settings | None = None,
    client: JiraClient | None = None,
) -> WorklogBundle:
    """Fetch report-day worklogs for roster designers. Never raises."""
    s = settings or get_settings()
    if not s.jira_configured:
        return WorklogBundle.unavailable("JIRA_* not configured")

    query_people = [
        p
        for p in roster_rows
        if effective_status(
            p.status,
            p.leave_until,
            report_date,
            leave_from=getattr(p, "leave_from", None),
        )
        != "on_leave"
    ]
    id_map = {str(p.jira_account_id): p for p in query_people if getattr(p, "jira_account_id", None)}
    if not id_map:
        return WorklogBundle(available=True, note="no Jira account ids on roster")

    try:
        jira = client or JiraClient(s)
    except Exception as exc:  # noqa: BLE001 — report-only digest
        return WorklogBundle.unavailable(f"{type(exc).__name__}: {exc}")

    rows: list[dict] = []
    source = None
    tempo_note = None
    if s.tempo_configured:
        try:
            rows = _rows_from_tempo(id_map, report_date, jira, s)
            source = "tempo"
        except Exception as exc:  # noqa: BLE001 — fall back to Jira worklogs
            tempo_note = f"tempo: {type(exc).__name__}: {exc}"
            rows = []
    if not rows:
        try:
            rows = jira.search_worklogged_on(list(id_map.keys()), report_date)
            if rows:
                source = "jira"
        except Exception as exc:  # noqa: BLE001
            if source != "tempo":
                return WorklogBundle.unavailable(
                    tempo_note or f"{type(exc).__name__}: {exc}"
                )

    bundle = WorklogBundle(available=True, note=tempo_note)
    if source:
        bundle.note = (bundle.note + f" · source={source}") if bundle.note else f"source={source}"
    for row in rows:
        person = id_map.get(row.get("author_account_id") or "")
        if person is None:
            continue
        doc: Document = row["document"]
        raw = doc.raw or {}
        ticket_dict = {
            "issue_type": raw.get("issue_type") or doc.jira_issue_type,
            "project_key": raw.get("project_key") or doc.project_hint,
            "components": raw.get("components") or [],
            "original_hours": raw.get("original_hours") or 0,
            "spent_hours": raw.get("spent_hours") or 0,
        }
        if not is_design_owned_ticket(ticket_dict):
            continue
        pk = (raw.get("project_key") or doc.project_hint or "").upper()
        if pk == VACSICK_PROJECT_KEY:
            continue
        stale = row.get("status_unchanged_days")
        trow = _ticket_from_doc(doc, hours=float(row.get("hours") or 0), stale_days=stale)
        name = person.full_name
        bundle.tickets.append((name, trow))
        hrs = trow.hours or 0.0
        bundle.hours_by_person[name] = round(bundle.hours_by_person.get(name, 0.0) + hrs, 2)
        proj_key = trow.project_key or ""
        if proj_key:
            pkey = (name, proj_key)
            bundle.hours_by_person_project[pkey] = round(
                bundle.hours_by_person_project.get(pkey, 0.0) + hrs, 2
            )
    return bundle


def _rows_from_tempo(
    id_map: dict[str, object],
    report_date: date,
    jira: JiraClient,
    settings: Settings,
) -> list[dict]:
    """Tempo hours + Jira issue details. Tempo payloads usually lack issue keys."""
    logs = TempoClient(settings).list_worklogs(
        from_date=report_date,
        to_date=report_date,
        account_ids=list(id_map.keys()),
    )
    if not logs:
        return []
    issue_ids = sorted({str(w.get("issue_id")) for w in logs if w.get("issue_id")})
    docs = jira.search_by_ids(issue_ids, event_date=report_date, expand=["changelog"])
    by_id: dict[str, Document] = {}
    by_key: dict[str, Document] = {}
    for doc in docs:
        raw = doc.raw or {}
        if raw.get("id"):
            by_id[str(raw["id"])] = doc
        if raw.get("key"):
            by_key[str(raw["key"]).upper()] = doc
    rows: list[dict] = []
    for w in logs:
        aid = w.get("account_id")
        if aid not in id_map:
            continue
        hours = float(w.get("hours") or 0)
        if hours <= 0:
            continue
        doc = None
        if w.get("issue_id"):
            doc = by_id.get(str(w["issue_id"]))
        if doc is None and w.get("issue_key"):
            doc = by_key.get(str(w["issue_key"]).upper())
        if doc is None:
            continue
        rows.append(
            {
                "document": doc,
                "author_account_id": aid,
                "hours": round(hours, 2),
                "status_unchanged_days": status_unchanged_days(doc.raw, report_date),
            }
        )
    return rows


def zero_logged_range_label(
    person: str,
    project: str,
    report_date: date,
    *,
    hours_today: float,
    prior_hours: list[tuple[date, float]] | None = None,
) -> str | None:
    """Consecutive trailing 0h reporting days, e.g. '11–12 Aug' or '12 Aug'."""
    if hours_today > 0:
        return None

    def _fmt(day: date) -> str:
        return day.strftime("%-d %b").replace(" 0", " ")

    start_zero = report_date
    by_day = {d: h for d, h in (prior_hours or [])}
    cursor = report_date - timedelta(days=1)
    scanned = 0
    while scanned < LOOKBACK_DAYS:
        if cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
            continue
        scanned += 1
        if cursor not in by_day:
            break
        if (by_day.get(cursor) or 0) > 0:
            break
        start_zero = cursor
        cursor -= timedelta(days=1)
    if start_zero == report_date:
        return _fmt(report_date)
    if start_zero.month == report_date.month and start_zero.year == report_date.year:
        return f"{start_zero.day}–{_fmt(report_date)}"
    return f"{_fmt(start_zero)}–{_fmt(report_date)}"


def attach_worklogs(
    digest: dict,
    bundle: WorklogBundle,
    *,
    report_date: date,
    registry: ProjectRegistry | None = None,
    settings: Settings | None = None,
    client: JiraClient | None = None,
    prior_hours: dict[tuple[str, str], list[tuple[date, float]]] | None = None,
) -> None:
    """Mutate status rows with ticket lists; add unreported logged projects."""
    digest["jira_available"] = bundle.available
    digest["jira_note"] = bundle.note
    digest["hours_by_person"] = dict(bundle.hours_by_person) if bundle.available else {}

    if not bundle.available:
        for row in digest.get("status") or []:
            row.pop("tickets", None)
            row.pop("ticket_hours", None)
            row.pop("no_time_logged", None)
            row.pop("no_ticket_found", None)
        return

    by_person_key: dict[tuple[str, str], list[TicketRow]] = {}
    for name, trow in bundle.tickets:
        project = _canonical_project(registry, trow, trow.project_key or "Unassigned")
        by_person_key.setdefault((name, project.lower()), []).append(trow)

    named_needed: dict[str, set[str]] = {}  # person → keys mentioned with no worklog
    for row in digest.get("status") or []:
        person = (row.get("person") or "").strip()
        project = (row.get("project") or "").strip()
        text = f"{row.get('done') or ''} {row.get('next') or ''}"
        mentioned = extract_ticket_keys(text)
        logged = by_person_key.get((person, project.lower()), [])
        logged_keys = {t.key for t in logged}
        for key in mentioned:
            if key not in logged_keys:
                named_needed.setdefault(person, set()).add(key)

    named_docs: dict[str, Document] = {}
    all_named = sorted({k for keys in named_needed.values() for k in keys})
    if all_named:
        try:
            jira = client or JiraClient(settings or get_settings())
            for doc in jira.search_by_keys(
                all_named, event_date=report_date, expand=["changelog"]
            ):
                key = ((doc.raw or {}).get("key") or doc.external_id or "").upper()
                named_docs[key] = doc
        except Exception:  # noqa: BLE001
            named_docs = {}

    seen_person_project: set[tuple[str, str]] = set()
    for row in digest.get("status") or []:
        person = (row.get("person") or "").strip()
        project = (row.get("project") or "").strip()
        seen_person_project.add((person, project.lower()))
        logged = list(by_person_key.get((person, project.lower()), []))
        text = f"{row.get('done') or ''} {row.get('next') or ''}"
        mentioned = extract_ticket_keys(text)
        logged_keys = {t.key for t in logged}
        extra: list[TicketRow] = []
        for key in mentioned:
            if key in logged_keys:
                continue
            doc = named_docs.get(key)
            if doc is None:
                extra.append(
                    TicketRow(
                        key=key,
                        summary="",
                        status="",
                        hours=None,
                        url=_browse_url(key, settings),
                        stale_days=None,
                        project_key=_project_key_from_issue(key),
                        project_name=None,
                        named_only=True,
                    )
                )
                continue
            stale = status_unchanged_days(doc.raw, report_date)
            extra.append(
                _ticket_from_doc(doc, hours=None, stale_days=stale, named_only=True)
            )
        tickets = logged + extra
        hours_sum = round(sum(t.hours or 0.0 for t in tickets if t.hours), 2)
        row["tickets"] = [_ticket_to_dict(t) for t in tickets]
        row["ticket_hours"] = hours_sum
        row["no_ticket_found"] = not tickets
        no_time = hours_sum <= 0
        row["no_time_logged"] = None
        if no_time:
            label = zero_logged_range_label(
                person,
                project,
                report_date,
                hours_today=hours_sum,
                prior_hours=(prior_hours or {}).get((person, project.lower())),
            )
            if label:
                row["no_time_logged"] = f"No time logged on this project {label}."
        digest["hours_by_person_project"] = digest.get("hours_by_person_project") or {}
        digest["hours_by_person_project"][f"{person}|{project}"] = hours_sum

    # Projects they logged on but didn't mention in the daily.
    extras: list[dict] = []
    for (person, proj_key), trows in by_person_key.items():
        # proj_key here is canonical lower name
        canon = trows[0] and _canonical_project(
            registry, trows[0], trows[0].project_key or "Unassigned"
        )
        if (person, canon.lower()) in seen_person_project:
            continue
        hours_sum = round(sum(t.hours or 0.0 for t in trows if t.hours), 2)
        extras.append(
            {
                "person": person,
                "project": canon,
                "done": "",
                "next": "",
                "tickets": [_ticket_to_dict(t) for t in trows],
                "ticket_hours": hours_sum,
                "no_ticket_found": False,
                "no_time_logged": None,
                "untracked": False,
            }
        )
        seen_person_project.add((person, canon.lower()))
    if extras:
        digest.setdefault("status", []).extend(extras)


def _project_key_from_issue(key: str) -> str | None:
    if "-" not in key:
        return None
    return key.split("-", 1)[0].upper()


def _browse_url(key: str, settings: Settings | None) -> str | None:
    s = settings or get_settings()
    base = (s.jira_base_url or "").rstrip("/")
    return f"{base}/browse/{key}" if base and key else None


def _ticket_to_dict(t: TicketRow) -> dict:
    return {
        "key": t.key,
        "summary": t.summary,
        "status": t.status,
        "hours": t.hours,
        "hours_label": format_hours(t.hours),
        "url": t.url,
        "stale_days": t.stale_days,
        "project_key": t.project_key,
        "original_hours": t.original_hours,
        "spent_hours": t.spent_hours,
        "named_only": t.named_only,
        "status_class": _status_class(t.status),
    }


def _status_class(status: str) -> str:
    low = (status or "").strip().lower()
    if low in {"in progress", "analysis"}:
        return "prog"
    if low in {"in review", "pm review", "internal review"}:
        return "rev"
    if low in {"done", "closed", "resolved"}:
        return "done"
    if "wait" in low or "client action" in low:
        return "wait"
    return ""
