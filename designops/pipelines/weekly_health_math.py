"""Pure helpers for A2 Weekly Project Health & Budget.

Scope filter, burn % colour bands, status grouping, ageing, and RAG sort —
all code, never LLM.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from designops.core.enums import TIME_LOG_BUCKET_TYPES

DESIGN_COMPONENTS: frozenset[str] = frozenset(
    {"design", "ux", "ux/ui", "cro-ux", "cro/ux", "ui"}
)

EPIC_TYPES: frozenset[str] = frozenset({"epic"})

# Status display buckets (prototype order).
STATUS_GROUP_ORDER: list[tuple[str, frozenset[str]]] = [
    ("In Progress", frozenset({"in progress", "analysis", "pm action", "pm review", "qa"})),
    ("Client Action", frozenset({"client action"})),
    ("Work underway — logged this cycle", frozenset()),  # assigned dynamically
    ("Log time — ongoing buckets", frozenset({"log time"})),
    (
        "To Do / New — not started",
        frozenset({"to do", "new", "ready to do", "ready for development", "backlog"}),
    ),
    ("Not started", frozenset()),
    ("On Hold", frozenset({"on hold", "blocked by", "blocked"})),
    ("Done", frozenset({"done", "closed", "resolved"})),
]

COLLAPSE_GROUPS: frozenset[str] = frozenset(
    {
        "To Do / New — not started",
        "Done",
        "Log time — ongoing buckets",
        "Backlog — not started",
        "Not started",
    }
)

CLIENT_ACTION_STATUSES: frozenset[str] = frozenset({"client action"})

_KEY_NUM_RE = re.compile(r"^[A-Z][A-Z0-9]*-(\d+)$", re.I)


def pct_class(logged: float, estimate: float, *, is_done: bool) -> str:
    """Colour band for burn % (spec §3). Colour the number only."""
    if estimate <= 0 or logged == 0:
        return "mut"
    if is_done:
        return "done"
    p = logged / estimate
    if p >= 1.00:
        return "bad"
    if p >= 0.85:
        return "warn"
    if p >= 0.65:
        return "watch"
    return "ok"


def burn_pct(logged: float, estimate: float) -> int | None:
    if estimate <= 0 or logged <= 0:
        return None
    return round(logged / estimate * 100)


def fmt_hours(v: float | None) -> str:
    """Display hours: '904h', '231.58h', or 'n/a' — never float-repr noise."""
    if v is None:
        return "n/a"
    r = round(float(v), 2)
    if r == int(r):
        return f"{int(r)}h"
    return f"{r:g}h"


def jira_browse_url(key: str | None, *, base: str | None = None) -> str | None:
    """Build a browse URL for a Jira issue or project key."""
    from designops.core.config import get_settings

    base = (base or get_settings().jira_base_url or "").rstrip("/")
    key = (key or "").strip()
    if not base or not key:
        return None
    return f"{base}/browse/{key}"


def project_jira_links(
    *,
    jira_project_key: str | None,
    jira_scope: dict | None = None,
    base: str | None = None,
) -> dict:
    """Epic and/or project browse links for a health card header."""
    scope = jira_scope or {}
    epic_key = (scope.get("epic_key") or "").strip() or None
    project_key = (jira_project_key or "").strip() or None
    return {
        "epic_key": epic_key,
        "epic_url": jira_browse_url(epic_key, base=base),
        "jira_project_key": project_key,
        "jira_project_url": jira_browse_url(project_key, base=base),
        # Prefer epic link for the header; fall back to project board.
        "jira_url": jira_browse_url(epic_key or project_key, base=base),
    }


def is_done_status(status: str | None, status_category: str | None = None) -> bool:
    if status_category and status_category.lower() in {"done", "complete"}:
        return True
    s = (status or "").strip().lower()
    return s in {"done", "closed", "resolved"}


def status_chip_class(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s in {"in progress", "analysis"}:
        return "ip"
    if s == "client action":
        return "ca"
    if s == "log time":
        return "lt"
    if s in {"done", "closed", "resolved"}:
        return "dn"
    if s in {"new"}:
        return "new"
    return "td"


def group_label_for_status(status: str | None, *, logged: float = 0.0) -> str:
    s = (status or "").strip().lower()
    # Hours logged while still New/Backlog → surface as underway (prototype Tobis)
    if logged > 0 and s in {"new", "backlog", "to do", "ready to do"}:
        return "Work underway — logged this cycle"
    for label, members in STATUS_GROUP_ORDER:
        if s in members:
            return label
    if not s:
        return "To Do / New — not started"
    if is_done_status(status):
        return "Done"
    return status or "Other"


def design_roster_emails(people: list) -> set[str]:
    """Exact emails from the design roster (Person rows)."""
    out: set[str] = set()
    for p in people:
        for e in getattr(p, "emails", None) or []:
            if e:
                out.add(str(e).lower().strip())
    return out


def _components_match(components: list[str] | None) -> bool:
    for c in components or []:
        if str(c).strip().lower() in DESIGN_COMPONENTS:
            return True
    return False


def is_timelog_bucket(ticket: dict) -> bool:
    itype = (ticket.get("issue_type") or "").strip()
    if itype in TIME_LOG_BUCKET_TYPES:
        return True
    est = float(ticket.get("original_hours") or 0)
    logged = float(ticket.get("spent_hours") or 0)
    if 0 < est <= 1 and logged > 3 * est:
        return True
    return False


def is_epic(ticket: dict) -> bool:
    return (ticket.get("issue_type") or "").strip().lower() in EPIC_TYPES


def in_design_scope(ticket: dict, roster_emails: set[str]) -> bool:
    """Include if DESIGN/UX component OR assignee email on design roster.

    Then exclude epics, time-log buckets, and non-design assignees without
    a design component.
    """
    if is_epic(ticket) or is_timelog_bucket(ticket):
        return False
    comps_ok = _components_match(ticket.get("components"))
    email = (ticket.get("assignee_email") or "").lower().strip()
    roster_ok = bool(email) and email in roster_emails
    if comps_ok or roster_ok:
        return True
    return False


def apply_jira_scope(tickets: list[dict], jira_scope: dict | None) -> list[dict]:
    """Optional scope: prefer epic descendants, else legacy key-number window.

    ``epic_key`` (e.g. UOM-481) keeps the epic and any ticket whose parent chain
    reaches that epic — so when children are added/removed under the epic, the
    weekly health burn follows automatically.
    """
    if not jira_scope:
        return tickets

    epic = (jira_scope.get("epic_key") or "").strip().upper()
    if epic:
        parent_of: dict[str, str] = {}
        for t in tickets:
            key = (t.get("key") or "").strip().upper()
            if not key:
                continue
            parent = (t.get("parent_key") or "").strip().upper()
            if parent:
                parent_of[key] = parent

        def under_epic(key: str) -> bool:
            k = key
            seen: set[str] = set()
            while k and k not in seen:
                if k == epic:
                    return True
                seen.add(k)
                k = parent_of.get(k, "")
            return False

        return [
            t
            for t in tickets
            if (t.get("key") or "").strip().upper() == epic
            or under_epic((t.get("key") or "").strip().upper())
        ]

    # Legacy numeric window (kept for older seeds / one-off filters).
    key_min = jira_scope.get("key_min")
    key_max = jira_scope.get("key_max")
    if key_min is None and key_max is None:
        return tickets
    out = []
    for t in tickets:
        key = t.get("key") or ""
        m = _KEY_NUM_RE.match(key)
        if not m:
            continue
        n = int(m.group(1))
        if key_min is not None and n < int(key_min):
            continue
        if key_max is not None and n > int(key_max):
            continue
        out.append(t)
    return out


def epic_subtitle(tickets: list[dict], jira_scope: dict | None) -> str | None:
    """If scoped to an epic, use that epic's current Jira summary as subtitle."""
    if not jira_scope:
        return None
    epic = (jira_scope.get("epic_key") or "").strip().upper()
    if not epic:
        return None
    for t in tickets:
        if (t.get("key") or "").strip().upper() == epic:
            summary = (t.get("summary") or "").strip()
            return summary or None
    return None


def working_days_between(start: date, end: date) -> int:
    """Inclusive working-day count Mon–Fri from start to end (end ≥ start)."""
    if end < start:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def client_action_ageing(
    tickets: list[dict],
    *,
    as_of: date,
    threshold_working_days: int = 3,
) -> list[dict]:
    """Tickets in Client Action older than threshold working days."""
    aged = []
    for t in tickets:
        status = (t.get("status") or "").strip().lower()
        if status not in CLIENT_ACTION_STATUSES:
            continue
        entered = t.get("client_action_since")
        if isinstance(entered, str):
            try:
                entered = date.fromisoformat(entered[:10])
            except ValueError:
                entered = None
        if not isinstance(entered, date):
            continue
        age = working_days_between(entered, as_of)
        if age > threshold_working_days:
            aged.append({**t, "working_days_in_status": age})
    return aged


def first_name(display: str | None, email: str | None = None) -> str:
    if display:
        return display.split()[0]
    if email:
        return email.split("@")[0].split(".")[0].title()
    return "—"


def enrich_ticket(raw: dict, *, as_of: date | None = None) -> dict:
    """Normalize a Jira Document.raw (or ticket dict) for the burn table."""
    t = dict(raw)
    est = float(t["original_hours"]) if t.get("original_hours") is not None else 0.0
    logged = float(t["spent_hours"]) if t.get("spent_hours") is not None else 0.0
    done = is_done_status(t.get("status"), t.get("status_category"))
    pct = burn_pct(logged, est)
    t["est"] = est
    t["logged"] = logged
    t["pct"] = pct
    t["pct_class"] = pct_class(logged, est, is_done=done)
    t["pct_display"] = f"{pct}%" if pct is not None else "—"
    t["is_done"] = done
    t["group"] = group_label_for_status(t.get("status"), logged=logged)
    t["st_class"] = status_chip_class(t.get("status"))
    t["owner"] = first_name(t.get("assignee_display"), t.get("assignee_email"))
    ca_since = None
    for e in t.get("status_entries") or []:
        if (e.get("to") or "").strip().lower() == "client action":
            try:
                ca_since = date.fromisoformat(str(e.get("at"))[:10])
            except (TypeError, ValueError):
                ca_since = None
            break
    t["client_action_since"] = ca_since
    if as_of and ca_since:
        t["working_days_in_status"] = working_days_between(ca_since, as_of)

    # Last status change date (any transition); fall back to Jira updated.
    status_since = None
    entries = t.get("status_entries") or []
    if entries:
        try:
            status_since = date.fromisoformat(str(entries[-1].get("at"))[:10])
        except (TypeError, ValueError):
            status_since = None
    if status_since is None and t.get("updated"):
        try:
            status_since = date.fromisoformat(str(t["updated"])[:10])
        except (TypeError, ValueError):
            status_since = None
    t["status_since"] = status_since
    days = None
    stale = ""
    if as_of and status_since:
        days = max(0, (as_of - status_since).days)
        if days >= 14:
            stale = "red"
        elif days >= 7:
            stale = "amber"
    t["days_in_status"] = days
    t["status_stale"] = stale
    return t


def _summarize_group(label: str, tickets: list[dict]) -> dict:
    est = round(sum(t["est"] for t in tickets), 1)
    logged = round(sum(t["logged"] for t in tickets), 1)
    owners = sorted({t["owner"] for t in tickets if t.get("owner") and t["owner"] != "—"})
    owner_label = " / ".join(owners[:3]) if owners else "mixed"
    if len(owners) > 3:
        owner_label = "mixed"
    names = [t.get("summary") or t.get("key") or "" for t in tickets]
    if len(names) == 1:
        summary = names[0]
    elif len(names) <= 3:
        summary = ", ".join(names)
    else:
        summary = f"{names[0]} … +{len(names) - 1} more"
    # Collapse large not-started/done buckets; keep small active sets expanded
    collapse = (label in COLLAPSE_GROUPS and len(tickets) >= 3) or len(tickets) > 8
    return {
        "status": label,
        "count": len(tickets),
        "est_hours": est,
        "log_hours": logged,
        "collapsed": collapse,
        "tickets": tickets,
        "sub_summary": summary,
        "sub_owner": owner_label,
        "st_class": status_chip_class(tickets[0].get("status") if tickets else label),
    }


def group_burn_tickets(tickets: list[dict]) -> list[dict]:
    """Group enriched tickets for the burn table."""
    buckets: dict[str, list[dict]] = {}
    order_labels = [lab for lab, _ in STATUS_GROUP_ORDER]
    for t in tickets:
        buckets.setdefault(t["group"], []).append(t)

    def sort_key(label: str) -> tuple[int, str]:
        try:
            return (order_labels.index(label), label.lower())
        except ValueError:
            return (50, label.lower())

    groups = []
    for label in sorted(buckets.keys(), key=sort_key):
        rows = buckets[label]
        # Within group: highest burn first for active work
        rows.sort(key=lambda x: (-(x["pct"] or -1), x.get("key") or ""))
        groups.append(_summarize_group(label, rows))
    return groups


def build_project_burn(
    *,
    display_name: str,
    subtitle: str | None,
    signed_estimate_h: float | None,
    agreement: dict | None,
    tickets: list[dict],
    as_of: date,
) -> dict:
    """Assemble code-side project card numbers (LLM fills health/verdict/highlights)."""
    agreement = agreement or {}
    _ = agreement  # SOW facts attached by orchestrator; invoice tile is Fairwind-only
    enriched = [enrich_ticket(t, as_of=as_of) for t in tickets]
    groups = group_burn_tickets(enriched)
    total_est = round(sum(t["est"] for t in enriched), 1)
    total_logged = round(sum(t["logged"] for t in enriched), 1)
    over_est = sum(
        1
        for t in enriched
        if not t["is_done"] and t["est"] > 0 and t["logged"] >= t["est"]
    )
    in_client = sum(
        1 for t in enriched if (t.get("status") or "").lower() == "client action"
    )
    aged = client_action_ageing(enriched, as_of=as_of)
    done_count = sum(1 for t in enriched if t.get("is_done"))
    done_est_h = round(sum(t["est"] for t in enriched if t.get("is_done")), 1)
    ticket_count = len(enriched)
    done_pct = round(done_count / ticket_count * 100) if ticket_count else 0
    # Progress by estimate: done tickets' estimates vs all scoped estimates.
    done_est_pct = round(done_est_h / total_est * 100) if total_est else None

    signed_pct = None
    if signed_estimate_h and signed_estimate_h > 0:
        signed_pct = round(total_logged / signed_estimate_h * 100)

    coverage_pct = None
    if signed_estimate_h and signed_estimate_h > 0 and total_est > 0:
        coverage_pct = round(total_est / signed_estimate_h * 100)

    # Status heuristic (code) — LLM may refine via health.clean.
    # Badge shows warning words, not colour names: At risk / Watch / On track.
    if over_est >= 2 or len(aged) >= 3:
        rag = "r"
        rag_label = "At risk"
    elif over_est >= 1 or in_client >= 3 or len(aged) >= 1:
        rag = "a"
        rag_label = "Watch"
    else:
        rag = "g"
        rag_label = "On track"

    # Invoice tile is filled only from Fairwind in the orchestrator — never from seeds.
    signed_sub = (
        "signed design hours"
        if signed_estimate_h is not None
        else "no signed estimate yet"
    )

    logged_sub = (
        f"{signed_pct}% of signed"
        if signed_pct is not None
        else (f"of {total_est:g}h Jira-planned (not signed)" if total_est else "—")
    )

    return {
        "display_name": display_name,
        "subtitle": subtitle or "",
        "signed_estimate_h": signed_estimate_h,
        "signed_estimate_display": fmt_hours(signed_estimate_h),
        "signed_estimate_muted": signed_estimate_h is None,
        "signed_estimate_sub": signed_sub,
        "logged_h": total_logged,
        "logged_sub": logged_sub,
        "invoiced_label": "n/a",
        "invoiced_muted": True,
        "invoiced_sub": "—",
        "invoice_notes": None,
        "total_est": total_est,
        "total_logged": total_logged,
        "ticket_count": ticket_count,
        "done_count": done_count,
        "done_pct": done_pct,
        "done_est_h": done_est_h,
        "done_est_pct": done_est_pct,
        "coverage_pct": coverage_pct,
        "over_est_count": over_est,
        "client_action_count": in_client,
        "aged_client_action": aged,
        "groups": groups,
        "tickets": enriched,
        "rag": rag,
        "rag_label": rag_label,
        "pending": False,
        # Filled by LLM
        "verdict": "",
        "health": {"clean": True, "text": ""},
        "highlights": [],
    }


def glance_kpis(projects: list[dict]) -> dict:
    reported = [p for p in projects if not p.get("pending")]
    total = len(projects)
    return {
        "reported": len(reported),
        "total": total,
        "client_action": sum(p.get("client_action_count") or 0 for p in reported),
        "over_est": sum(p.get("over_est_count") or 0 for p in reported),
        "action_items": sum(len(p.get("highlights") or []) for p in reported),
    }


def rag_sort_key(project: dict) -> tuple:
    rag_rank = {"r": 0, "a": 1, "g": 2}
    return (
        1 if project.get("pending") else 0,
        rag_rank.get(project.get("rag"), 9),
        -int(project.get("over_est_count") or 0),
        -int(project.get("client_action_count") or 0),
        (project.get("display_name") or "").lower(),
    )


def doc_to_ticket(doc) -> dict:
    raw = getattr(doc, "raw", None) or {}
    if isinstance(doc, dict) and "key" in doc:
        return dict(doc)
    return {
        "key": raw.get("key") or getattr(doc, "external_id", None),
        "summary": raw.get("summary"),
        "status": raw.get("status"),
        "status_category": raw.get("status_category"),
        "original_hours": raw.get("original_hours"),
        "spent_hours": raw.get("spent_hours"),
        "remaining_hours": raw.get("remaining_hours"),
        "assignee_email": raw.get("assignee_email"),
        "assignee_display": raw.get("assignee_display"),
        "assignee_account_id": raw.get("assignee_account_id"),
        "components": raw.get("components") or [],
        "issue_type": raw.get("issue_type") or getattr(doc, "jira_issue_type", None),
        "project_key": raw.get("project_key") or getattr(doc, "project_hint", None),
        "parent_key": raw.get("parent_key"),
        "status_entries": raw.get("status_entries") or [],
        "comments": raw.get("comments") or [],
        "updated": raw.get("updated"),
        "url": getattr(doc, "url", None),
    }
