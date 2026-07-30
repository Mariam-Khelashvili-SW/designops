"""Pure helpers for A3 Weekly Planning Board.

Scope is code: availability, planned/blocked hours, capacity bands, and KPIs
are decided here — never by the LLM.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from designops.core.enums import PersonStatus, TIME_LOG_BUCKET_TYPES
from designops.core.identity import effective_status

# --- Status sets (A3 §6) -----------------------------------------------------

BLOCKED_STATUSES: frozenset[str] = frozenset({"on hold", "blocked by", "blocked"})

ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        "in progress",
        "client action",
        "pm action",
        "pm review",
        "to do",
        "new",
        "analysis",
        "ready to do",
        "ready for development",
        "qa",
        "backlog",
    }
)

# Display order for per-person status groups (blocked last).
STATUS_DISPLAY_ORDER: list[str] = [
    "In Progress",
    "Client Action",
    "PM Action",
    "PM Review",
    "To Do",
    "New",
    "Analysis",
    "Ready To Do",
    "Ready for development",
    "QA",
    "Backlog",
    "On Hold",
    "Blocked",
]

_STATUS_ORDER_INDEX = {s.lower(): i for i, s in enumerate(STATUS_DISPLAY_ORDER)}

# Canonical display names for known statuses.
_STATUS_CANON: dict[str, str] = {
    "in progress": "In Progress",
    "client action": "Client Action",
    "pm action": "PM Action",
    "pm review": "PM Review",
    "to do": "To Do",
    "new": "New",
    "analysis": "Analysis",
    "ready to do": "Ready To Do",
    "ready for development": "Ready for development",
    "qa": "QA",
    "backlog": "Backlog",
    "on hold": "On Hold",
    "blocked by": "Blocked",
    "blocked": "Blocked",
}

HARDWARE_PROJECT_PREFIX = "IMR"
HARDWARE_ISSUE_TYPES: frozenset[str] = frozenset({"in use"})

_TICKET_KEY_RE = re.compile(r"\b([A-Z]{2,}\d?-\d+)\b")
_RANGE_RE = re.compile(
    r"\b([A-Z]{2,}\d?)-(\d+)\s*(?:through|to|→|–|—|-)\s*(?:\1-)?(\d+)\b",
    re.IGNORECASE,
)

SPARE_THRESHOLD = 30.0  # planned_hours < 30 → SPARE (§8)


def week_friday(week_monday: date) -> date:
    return week_monday + timedelta(days=4)


def previous_friday(week_monday: date) -> date:
    """Friday before the week that starts on `week_monday` (plans source)."""
    return week_monday - timedelta(days=3)


def week_monday_on_or_before(d: date) -> date:
    """Monday of the ISO week containing `d` (Mon=0)."""
    return d - timedelta(days=d.weekday())


def resolve_week_monday(d: date) -> date:
    """Map a user-picked calendar day to the Planned-week Monday.

    - Mon–Fri → Monday of that same week (ISO).
    - Sat/Sun → the *next* Monday (weekends belong to the upcoming week).
    """
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return d + timedelta(days=(7 - d.weekday()))
    return week_monday_on_or_before(d)


def availability_marker(
    status: str,
    leave_until: date | None,
    week_monday: date,
) -> str:
    """AVAILABLE | PARTIAL | OUT for the week starting `week_monday`."""
    fri = week_friday(week_monday)
    eff = effective_status(status, leave_until, week_monday)
    if status == PersonStatus.OUT or eff == PersonStatus.OUT:
        return "OUT"
    if eff == PersonStatus.ON_LEAVE:
        if leave_until and week_monday <= leave_until < fri:
            return "PARTIAL"
        return "OUT"
    return "AVAILABLE"


def expand_ticket_ranges(text: str) -> list[str]:
    """Expand `KEY-a through KEY-b` (or `to`) into every key in between."""
    keys: list[str] = []
    for m in _RANGE_RE.finditer(text or ""):
        prefix, start_s, end_s = m.group(1).upper(), m.group(2), m.group(3)
        start, end = int(start_s), int(end_s)
        if end < start:
            start, end = end, start
        # Cap runaway ranges (typos) at 50 keys.
        if end - start > 50:
            end = start + 50
        keys.extend(f"{prefix}-{n}" for n in range(start, end + 1))
    return keys


def extract_ticket_keys(text: str) -> list[str]:
    """Extract Jira keys from Friday plan text (§4), including range expansion.

    Returns unique keys in first-seen order.
    """
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    candidates = expand_ticket_ranges(text) + [
        m.group(1) for m in _TICKET_KEY_RE.finditer(text)
    ]
    for key in candidates:
        canon = key.upper()
        if canon not in seen:
            seen.add(canon)
            ordered.append(canon)
    return ordered


def is_hardware_ticket(ticket: dict) -> bool:
    """IMR project or issue type `In Use` — never work, never load (§5)."""
    project = (ticket.get("project_key") or "").upper()
    if project.startswith(HARDWARE_PROJECT_PREFIX):
        return True
    itype = (ticket.get("issue_type") or "").strip().lower()
    return itype in HARDWARE_ISSUE_TYPES


def is_timelog_ticket(ticket: dict) -> bool:
    itype = (ticket.get("issue_type") or "").strip()
    return itype in TIME_LOG_BUCKET_TYPES


def is_excluded_ticket(ticket: dict) -> bool:
    return is_hardware_ticket(ticket) or is_timelog_ticket(ticket)


def normalize_status(status: str | None) -> str:
    if not status:
        return "Unknown"
    key = status.strip().lower()
    return _STATUS_CANON.get(key, status.strip())


def is_blocked_status(status: str | None) -> bool:
    if not status:
        return False
    return status.strip().lower() in BLOCKED_STATUSES


def is_active_status(status: str | None) -> bool:
    if not status:
        return False
    return status.strip().lower() in ACTIVE_STATUSES


def burn_pct(est: float | None, log: float | None) -> int | None:
    """Log / Est × 100, or None when either side is missing/zero (§7)."""
    if est is None or log is None:
        return None
    if est <= 0 or log <= 0:
        return None
    return round(log / est * 100)


def burn_pct_class(pct: int | None) -> str:
    if pct is None:
        return "mut"
    if pct >= 100:
        return "over"
    if pct >= 80:
        return "hi"
    if pct >= 60:
        return "mid"
    if pct >= 40:
        return "lo"
    return "ok"


def ticket_left_hours(ticket: dict) -> float:
    h = ticket.get("remaining_hours")
    if h is None:
        return 0.0
    return float(h)


def classify_capacity_band(
    *,
    planned_hours: float,
    blocked_hours: float,
    capacity: float,
    availability: str,
) -> dict:
    """Return band metadata for the capacity board (§8).

    Keys: band, flag, bar_fill, bar_pct, meta_class
    Bar: track = capacity (40h); fill length = planned ÷ capacity, capped at 100%.
    """
    if availability == "OUT":
        return {
            "band": "OUT",
            "flag": "Out",
            "bar_fill": "",
            "bar_pct": 0,
            "meta_class": "out",
        }
    if planned_hours <= 0:
        if blocked_hours > 0:
            return {
                "band": "IDLE",
                "flag": "Blocked → idle",
                "bar_fill": "idle",
                "bar_pct": 4,
                "meta_class": "idle",
            }
        return {
            "band": "IDLE",
            "flag": "Idle",
            "bar_fill": "idle",
            "bar_pct": 0,
            "meta_class": "idle",
        }
    ratio = (planned_hours / capacity) if capacity else 0.0
    if planned_hours < SPARE_THRESHOLD:
        spare = max(0.0, capacity - planned_hours)
        pct = min(100, round(ratio * 100))
        return {
            "band": "SPARE",
            "flag": f"Spare ~{round(spare):.0f}h" if spare == int(spare) else f"Spare ~{spare:g}h",
            "bar_fill": "idle",
            "bar_pct": max(4, pct),
            "meta_class": "idle",
        }
    if planned_hours <= capacity:
        pct = min(100, round(ratio * 100))
        return {
            "band": "AT_CAPACITY",
            "flag": "At capacity",
            "bar_fill": "bal",
            "bar_pct": pct,
            "meta_class": "bal",
        }
    # Over-planned — flag shows no magnitude (§8)
    hot = planned_hours >= capacity * 2
    return {
        "band": "OVER_PLANNED",
        "flag": "Over-planned",
        "bar_fill": "hot" if hot else "over",
        "bar_pct": 100,
        "meta_class": "over",
    }


def load_flags(
    *,
    booked_hours: float,
    normal_hours: float,
    availability: str,
    has_friday_plan: bool,
) -> tuple[bool, bool]:
    """Legacy (overload, idle) — kept for older callers/tests.

    Prefer classify_capacity_band for new board logic.
    """
    overload = booked_hours > normal_hours
    idle = (
        availability == "AVAILABLE"
        and not has_friday_plan
        and booked_hours <= 0.0
    )
    return overload, idle


def booked_hours_from_docs(jira_docs: list) -> float:
    """Legacy: Σ remaining over all docs (pre-planning-board). Prefer planned_and_blocked."""
    total = 0.0
    for d in jira_docs:
        raw = getattr(d, "raw", None) or {}
        h = raw.get("remaining_hours")
        if h is None and raw.get("remaining_seconds") is not None:
            h = float(raw["remaining_seconds"]) / 3600.0
        if h is not None:
            total += float(h)
    return round(total, 2)


def doc_to_ticket(doc) -> dict:
    """Normalize a Jira Document (or raw dict) into a ticket display dict."""
    if isinstance(doc, dict) and "key" in doc and "remaining_hours" in doc:
        # Already a ticket-shaped dict
        t = dict(doc)
        t.setdefault("status", None)
        return t
    raw = getattr(doc, "raw", None) or {}
    return {
        "key": raw.get("key") or getattr(doc, "external_id", None),
        "summary": raw.get("summary") or getattr(doc, "title", None),
        "status": raw.get("status"),
        "original_hours": raw.get("original_hours"),
        "spent_hours": raw.get("spent_hours"),
        "remaining_hours": raw.get("remaining_hours"),
        "url": getattr(doc, "url", None) or raw.get("url"),
        "project_key": raw.get("project_key"),
        "project_name": raw.get("project_name"),
        "issue_type": raw.get("issue_type") or getattr(doc, "jira_issue_type", None),
    }


def filter_work_tickets(tickets: list[dict]) -> list[dict]:
    """Drop hardware / time-log buckets (§5)."""
    return [t for t in tickets if not is_excluded_ticket(t)]


def planned_and_blocked(tickets: list[dict]) -> tuple[float, float]:
    """Σ Left for non-blocked vs blocked planned tickets (§2–3)."""
    planned = 0.0
    blocked = 0.0
    for t in tickets:
        left = ticket_left_hours(t)
        if is_blocked_status(t.get("status")):
            blocked += left
        else:
            planned += left
    return round(planned, 2), round(blocked, 2)


def enrich_ticket(ticket: dict) -> dict:
    """Add burn % and display helpers onto a ticket dict."""
    t = dict(ticket)
    est = t.get("original_hours")
    log = t.get("spent_hours")
    pct = burn_pct(est, log)
    t["burn_pct"] = pct
    t["burn_class"] = burn_pct_class(pct)
    t["status_display"] = normalize_status(t.get("status"))
    t["blocked"] = is_blocked_status(t.get("status"))
    return t


def group_tickets_by_status(tickets: list[dict]) -> list[dict]:
    """Group enriched tickets by status in STATUS_DISPLAY_ORDER (§6)."""
    buckets: dict[str, list[dict]] = {}
    for t in tickets:
        enriched = enrich_ticket(t)
        label = enriched["status_display"]
        buckets.setdefault(label, []).append(enriched)

    def sort_key(label: str) -> tuple[int, str]:
        idx = _STATUS_ORDER_INDEX.get(label.lower(), 100)
        return (idx, label.lower())

    groups = []
    for label in sorted(buckets.keys(), key=sort_key):
        rows = sorted(
            buckets[label],
            key=lambda r: (
                (r.get("project_name") or r.get("project_key") or "").lower(),
                r.get("key") or "",
            ),
        )
        sum_est = sum(float(r["original_hours"] or 0) for r in rows)
        sum_log = sum(float(r["spent_hours"] or 0) for r in rows)
        sum_left = sum(ticket_left_hours(r) for r in rows)
        groups.append(
            {
                "status": label,
                "blocked": all(r.get("blocked") for r in rows)
                or is_blocked_status(label),
                "count": len(rows),
                "est_hours": round(sum_est, 2),
                "log_hours": round(sum_log, 2),
                "left_hours": round(sum_left, 2),
                "tickets": rows,
            }
        )
    return groups


def format_hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:g}h"


def default_flagline(band: str, *, blocked_hours: float = 0.0, **_kw) -> dict:
    """Code-side coaching label when LLM doesn't provide one."""
    if band == "OVER_PLANNED":
        return {
            "kind": "over",
            "lab": "Overloaded",
            "text": "Planned remaining work exceeds a normal week — trim or rebalance.",
        }
    if band == "IDLE" and blocked_hours > 0:
        return {
            "kind": "idle",
            "lab": "Unblock",
            "text": "Entire plan is blocked — zero workable hours until dependencies clear.",
        }
    if band == "IDLE":
        return {
            "kind": "idle",
            "lab": "Idle",
            "text": "No planned workable hours this week.",
        }
    if band == "SPARE":
        return {
            "kind": "idle",
            "lab": "Take work",
            "text": "Headroom available — clear place to route overflow.",
        }
    return {"kind": "", "lab": "", "text": ""}


def build_person_board_row(
    *,
    name: str,
    availability: str,
    tickets: list[dict],
    capacity: float,
    has_friday_plan: bool,
    friday_keys: list[str] | None = None,
    no_plan: bool = False,
) -> dict:
    """Assemble one person card + board row for the planning board."""
    work = filter_work_tickets(tickets)
    planned, blocked = planned_and_blocked(work)
    band_info = classify_capacity_band(
        planned_hours=planned,
        blocked_hours=blocked,
        capacity=capacity,
        availability=availability,
    )
    groups = group_tickets_by_status(work)
    flagline = default_flagline(
        band_info["band"], blocked_hours=blocked
    )
    if availability == "OUT":
        flagline = {"kind": "", "lab": "", "text": ""}

    avail_chip = "ok"
    avail_label = "Avail"
    if availability == "PARTIAL":
        avail_chip = "ok"
        avail_label = "Partial"
    elif availability == "OUT":
        avail_chip = "out"
        avail_label = "Out"

    return {
        "name": name,
        "availability": availability,
        "avail_chip": avail_chip,
        "avail_label": avail_label,
        "planned_hours": planned,
        "blocked_hours": blocked,
        "normal_hours": capacity,
        "unverified": False,
        "no_plan": no_plan,
        "friday_keys": friday_keys or [],
        "band": band_info["band"],
        "flag": band_info["flag"],
        "bar_fill": band_info["bar_fill"],
        "bar_pct": band_info["bar_pct"],
        "meta_class": band_info["meta_class"],
        "flagline": flagline,
        "status_groups": groups,
        "tickets": [enrich_ticket(t) for t in work],
        # Back-compat aliases used by older template bits / tests
        "booked_hours": planned,
        "overload": band_info["band"] == "OVER_PLANNED",
        "idle": band_info["band"] == "IDLE",
        "planned_this_week": None,
    }


def at_a_glance_kpis(people: list[dict], *, capacity: float = 40.0) -> dict:
    """Top-of-report KPIs (§9) — counts overlap; they do NOT sum to roster."""
    available = [p for p in people if p.get("availability") != "OUT"]
    fully_booked = sum(
        1
        for p in people
        if p.get("availability") != "OUT"
        and float(p.get("planned_hours") or 0) >= capacity
    )
    spare_capacity = sum(
        1
        for p in people
        if p.get("availability") == "AVAILABLE"
        and float(p.get("planned_hours") or 0) < SPARE_THRESHOLD
    )
    have_blocked = sum(1 for p in people if float(p.get("blocked_hours") or 0) > 0)
    on_leave = sum(1 for p in people if p.get("availability") == "OUT")
    team_planned = round(
        sum(float(p.get("planned_hours") or 0) for p in available), 1
    )
    team_capacity = round(len(available) * capacity, 1)
    ratio = round(team_planned / team_capacity, 1) if team_capacity else 0
    return {
        "fully_booked": fully_booked,
        "spare_capacity": spare_capacity,
        "have_blocked": have_blocked,
        "on_leave": on_leave,
        "team_planned": team_planned,
        "team_capacity": team_capacity,
        "team_ratio": ratio,
        # Keep legacy fields so older UI bits don't crash
        "team": len(people),
        "available": sum(1 for p in people if p.get("availability") == "AVAILABLE"),
        "partial": sum(1 for p in people if p.get("availability") == "PARTIAL"),
        "out": on_leave,
        "overloaded": fully_booked,
        "idle": spare_capacity,  # approximate; spare includes idle
        "overloaded_names": [
            p["name"]
            for p in people
            if p.get("band") == "OVER_PLANNED"
        ],
        "idle_names": [
            p["name"] for p in people if p.get("band") in ("IDLE", "SPARE")
        ],
    }


def board_sort_key(person: dict) -> tuple:
    """Board order: over-planned first by planned hours desc, then spare/idle, then out."""
    band_rank = {
        "OVER_PLANNED": 0,
        "AT_CAPACITY": 1,
        "SPARE": 2,
        "IDLE": 3,
        "OUT": 4,
    }
    return (
        band_rank.get(person.get("band"), 9),
        -float(person.get("planned_hours") or 0),
        (person.get("name") or "").lower(),
    )
