"""Daily Pulse post-processing — deduplication, grouping, KPIs (C1–C10).

Deterministic transforms applied after LLM synthesis and before HTML render.
The rendered body is the source of truth for at-a-glance counters.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from designops.core.models import Artifact, Pipeline, PipelineRun
from designops.core.enums import RunStatus
from designops.core.identity import effective_status
from designops.pipelines.weekly_availability import working_days_inclusive

ALLOWED_PROJECT_STATUSES = frozenset(
    {"on track", "waiting on you", "blocked on client", "cover needed", "internal"}
)

_COORD_CLAUSE_RE = re.compile(
    r"^(call|meeting|sync|catch-up|catch up|time logs? check)\b",
    re.I,
)
_OLGA_REVIEW_RE = re.compile(
    r"\b(olga|internal review|shared with olga|catch-up with olga|catch up with olga)\b",
    re.I,
)
_LEAVE_DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
    re.I,
)
_COVERAGE_PHRASE = "no coverage arrangement appears in the dailies"

_NOTE_TYPE_STALLED = "stalled"
_NOTE_TYPE_WAITING = "waiting"
_NOTE_TYPE_CONTEXT = "context"


def postprocess_digest(
    digest: dict,
    *,
    report_date: date,
    roster_rows: list | None = None,
    session: Session | None = None,
) -> None:
    """Apply all C1–C10 transforms in place."""
    _dedupe_escalations_by_issue(digest)
    _detect_olga_review_escalations(digest, report_date)
    _normalize_escalation_sources(digest)
    _assign_project_statuses(digest)
    _dedupe_and_type_project_notes(digest)
    _format_status_lines(digest)
    build_leave_badges(digest, roster_rows, report_date)
    _group_no_report_rows(digest)
    build_out_and_quiet(digest, roster_rows, report_date)
    _apply_silence_streaks(digest, report_date, session)
    _recompute_at_a_glance(digest)


# --- C1: dedupe Needs Olga by issue, not person --------------------------------


def _escalation_issue_key(e: dict) -> str:
    text = (e.get("text") or "").lower()
    agent = (e.get("agent_note") or "").lower()
    combined = f"{text} {agent}"
    if "cover" in combined or "leave" in combined or _COVERAGE_PHRASE in combined:
        dates = _LEAVE_DATE_RE.findall(combined)
        date_key = dates[0].lower() if dates else "unknown"
        return f"leave_coverage|{date_key}"
    if _OLGA_REVIEW_RE.search(combined):
        proj = (e.get("project") or "").strip().lower()
        return f"olga_review|{proj or 'general'}"
    proj = (e.get("project") or "").strip().lower()
    who = (e.get("who") or "").strip().lower()
    return f"other|{proj}|{who}|{hashlib.md5(text[:120].encode()).hexdigest()[:8]}"


def _dedupe_escalations_by_issue(digest: dict) -> None:
    raw = [
        e
        for e in (digest.get("escalations") or [])
        if isinstance(e, dict) and (e.get("text") or "").strip()
        and not str(e.get("text") or "").startswith("+")
    ]
    if not raw:
        digest["escalations"] = []
        return

    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in raw:
        key = _escalation_issue_key(e)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(e)

    merged: list[dict] = []
    for key in order:
        group = buckets[key]
        if len(group) == 1:
            row = dict(group[0])
            row.pop("agent_note", None)
            merged.append(row)
            continue
        if key.startswith("leave_coverage|"):
            merged.append(_merge_leave_coverage_escalations(group))
        else:
            primary = dict(group[0])
            for extra in group[1:]:
                _merge_escalation_sources(primary, extra)
            primary.pop("agent_note", None)
            merged.append(primary)

    digest["escalations"] = merged[:5]
    if len(raw) > 5:
        held = len(raw) - 5
        digest["escalations"].append(
            {
                "text": f"+{held} lower-priority items held — ask to see them.",
                "evidence": "— ranking cap",
                "why_ranked_here": "overflow after top 5",
            }
        )


def _merge_leave_coverage_escalations(group: list[dict]) -> dict:
    """One decision per leave date; people nested under affected projects."""
    affected: list[dict] = []
    sources: list[str] = []
    by_when = None
    for e in group:
        proj = (e.get("project") or "").strip()
        who = (e.get("who") or "").strip()
        line_parts = []
        if who:
            line_parts.append(who)
        text = e.get("text") or ""
        if proj:
            affected.append({"project": proj, "who": who, "detail": text})
        if e.get("by_when"):
            by_when = e["by_when"]
        _collect_sources(e, sources)

    # Build consolidated title from first item's leave date
    first_text = (group[0].get("text") or "").lower()
    dates = _LEAVE_DATE_RE.findall(first_text)
    date_label = dates[0] if dates else "upcoming leave"
    title = f"Coverage gap on {date_label} — no cover named in dailies"
    if len({a.get("project") for a in affected if a.get("project")}) > 1:
        title = f"Multiple projects lose designers on {date_label} — no cover named"

    return {
        "text": title,
        "affected": affected,
        "by_when": by_when or group[0].get("by_when"),
        "evidence": _join_sources(sources) or group[0].get("evidence"),
        "why_ranked_here": group[0].get("why_ranked_here") or "leave starts soon",
    }


def _merge_escalation_sources(primary: dict, extra: dict) -> None:
    sources: list[str] = []
    _collect_sources(primary, sources)
    _collect_sources(extra, sources)
    if sources:
        primary["evidence"] = _join_sources(sources)


def _collect_sources(row: dict, out: list[str]) -> None:
    ev = row.get("evidence")
    if isinstance(ev, str) and ev.strip():
        out.extend(_split_sources(ev))
    for a in row.get("affected") or []:
        if isinstance(a, dict) and a.get("source"):
            out.append(a["source"])


# --- C5: Olga review queue ---------------------------------------------------


def _detect_olga_review_escalations(digest: dict, report_date: date) -> None:
    existing_projects = {
        (e.get("project") or "").strip().lower()
        for e in (digest.get("escalations") or [])
        if _OLGA_REVIEW_RE.search((e.get("text") or "") + (e.get("agent_note") or ""))
    }
    for row in digest.get("status") or []:
        proj = (row.get("project") or "").strip()
        if not proj or proj.lower() in existing_projects:
            continue
        combined = f"{row.get('done') or ''} {row.get('next') or ''}"
        if not _OLGA_REVIEW_RE.search(combined):
            continue
        person = (row.get("person") or "").strip()
        by_when = _decide_by_date(report_date, days=2)
        digest.setdefault("escalations", []).append(
            {
                "text": f"{proj} screens are waiting on your internal review",
                "project": proj,
                "who": person,
                "by_when": f"Review by {by_when}",
                "evidence": f"daily report, {report_date.strftime('%-d %b')}",
                "why_ranked_here": "hand-off to Olga for review",
            }
        )
        existing_projects.add(proj.lower())
        row["_waiting_on_olga"] = True


def _decide_by_date(report_date: date, days: int = 2) -> str:
    d = report_date
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.strftime("%a %-d %b").replace(" 0", " ")


# --- C6: project status closed enum ------------------------------------------


def _assign_project_statuses(digest: dict) -> None:
    projects: dict[str, dict] = {}
    for row in digest.get("status") or []:
        proj = (row.get("project") or "").strip()
        if proj:
            projects.setdefault(proj.lower(), {"name": proj, "rows": []})["rows"].append(row)

    cover_projects = set()
    waiting_projects = set()
    for e in digest.get("escalations") or []:
        text = ((e.get("text") or "") + " " + (e.get("agent_note") or "")).lower()
        proj = (e.get("project") or "").strip()
        if "cover" in text or _COVERAGE_PHRASE in text:
            if proj:
                cover_projects.add(proj.lower())
            for a in e.get("affected") or []:
                if isinstance(a, dict) and a.get("project"):
                    cover_projects.add(a["project"].strip().lower())
        if _OLGA_REVIEW_RE.search(text) or "waiting on your" in text:
            if proj:
                waiting_projects.add(proj.lower())

    for r in digest.get("needs_review") or []:
        proj = (r.get("project") or "").strip().lower()
        if r.get("blocked") and proj:
            waiting_projects.add(proj)

    statuses: dict[str, str | None] = {}
    for key, info in projects.items():
        name = info["name"]
        rows = info["rows"]
        status = _infer_single_project_status(name, rows, cover_projects, waiting_projects)
        statuses[key] = status

    digest["project_statuses"] = statuses


def _infer_single_project_status(
    name: str,
    rows: list[dict],
    cover_projects: set[str],
    waiting_projects: set[str],
) -> str | None:
    key = name.lower()
    if key in cover_projects:
        return "cover needed"
    if key in waiting_projects or any(r.get("_waiting_on_olga") for r in rows):
        return "waiting on you"
    if "internal" in key or "scandiweb" in key:
        return "internal"
    for row in rows:
        nxt = (row.get("next") or "").lower()
        done = (row.get("done") or "").lower()
        if re.search(r"waiting for .*(client|feedback|nora|approval)", nxt + done):
            return "blocked on client"
        if re.search(r"\bblocked\b", nxt + done) and "client" in nxt + done:
            return "blocked on client"
    if rows:
        return "on track"
    return None


# --- C2 / C3: typed project notes, dedupe by body ----------------------------


def _normalize_note_body(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def _split_sources(evidence: str | None) -> list[str]:
    if not evidence:
        return []
    raw = evidence.strip()
    raw = re.sub(r"^[—\-]\s*", "", raw)
    parts = re.split(r"\s*[·×]\s*|\s*,\s*", raw)
    out: list[str] = []
    for p in parts:
        p = p.strip().lstrip("—- ").strip()
        if p and p not in out:
            out.append(p)
    return out


def _join_sources(sources: list[str]) -> str:
    uniq: list[str] = []
    for s in sources:
        s = s.strip().lstrip("—- ")
        if s and s not in uniq:
            uniq.append(s)
    return " · ".join(uniq) if uniq else ""


def _infer_note_type(text: str, *, rule_hint: str | None = None) -> str:
    low = (text or "").lower()
    if rule_hint in ("R4_repeated_next", "R6_rework"):
        return _NOTE_TYPE_STALLED
    if rule_hint in ("R2_client_wait_x_leave", "R5_report_language"):
        return _NOTE_TYPE_WAITING
    if re.search(r"next for \d+ run", low) or "come back for tweak" in low:
        return _NOTE_TYPE_STALLED
    if re.search(r"waiting|parked on|paused on|client input|feedback", low):
        return _NOTE_TYPE_WAITING
    return _NOTE_TYPE_CONTEXT


def _dedupe_and_type_project_notes(digest: dict) -> None:
    """Merge agent_note + heads_ups into project_notes with Stalled/Waiting/Context."""
    by_project: dict[str, list[dict]] = {}

    for row in digest.get("status") or []:
        proj = (row.get("project") or "").strip()
        if not proj:
            continue
        notes = row.get("agent_notes") or []
        if not notes and row.get("agent_note"):
            notes = [{"text": row["agent_note"], "evidence": None}]
        for n in notes:
            text = (n.get("text") if isinstance(n, dict) else str(n)).strip()
            if not text:
                continue
            ev = n.get("evidence") if isinstance(n, dict) else None
            by_project.setdefault(proj.lower(), []).append(
                {
                    "type": _infer_note_type(text),
                    "text": text,
                    "sources": _split_sources(ev),
                }
            )
        row.pop("agent_note", None)
        row.pop("agent_notes", None)

    for h in digest.get("heads_ups") or []:
        proj = (h.get("project") or "").strip()
        text = (h.get("text") or "").strip()
        if not proj or not text:
            continue
        by_project.setdefault(proj.lower(), []).append(
            {
                "type": _infer_note_type(text, rule_hint="R3_launch_proximity"),
                "text": text,
                "sources": _split_sources(h.get("evidence")),
            }
        )
    digest["heads_ups"] = []

    deduped: dict[str, list[dict]] = {}
    for proj_key, notes in by_project.items():
        seen: dict[str, dict] = {}
        for n in notes:
            body_key = _normalize_note_body(n["text"])
            if body_key in seen:
                existing = seen[body_key]
                for s in n.get("sources") or []:
                    if s not in existing["sources"]:
                        existing["sources"].append(s)
            else:
                seen[body_key] = {"type": n["type"], "text": n["text"], "sources": list(n.get("sources") or [])}
        deduped[proj_key] = list(seen.values())

    digest["project_notes"] = deduped


# --- C7: Done / Next line splitting ------------------------------------------


def _format_field_lines(text: str, *, max_lines: int = 3) -> list[str]:
    if not (text or "").strip():
        return []
    parts = [p.strip() for p in re.split(r";\s*", text) if p.strip()]
    if not parts:
        return [text.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and _COORD_CLAUSE_RE.match(part):
            merged[-1] = f"{merged[-1]}; {part}"
        else:
            merged.append(part)
    while len(merged) > max_lines:
        tail = merged.pop(-2)
        merged[-1] = f"{tail}; {merged[-1]}"
    return merged


def _format_status_lines(digest: dict) -> None:
    for row in digest.get("status") or []:
        done = (row.get("done") or "").strip()
        nxt = (row.get("next") or "").strip()
        if done:
            row["done_lines"] = _format_field_lines(done)
        if nxt:
            row["next_lines"] = _format_field_lines(nxt)


# --- C4: leave badges (data for render) --------------------------------------


def _working_days_until(from_date: date, to_date: date) -> int:
    """Working days after from_date through to_date (inclusive)."""
    if to_date <= from_date:
        return 0
    n = 0
    d = from_date + timedelta(days=1)
    while d <= to_date:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def leave_badge_for_person(
    person_name: str,
    roster_rows: list | None,
    report_date: date,
    *,
    within_working_days: int = 7,
) -> str | None:
    if not roster_rows:
        return None
    for p in roster_rows:
        if (getattr(p, "full_name", None) or "") != person_name:
            continue
        lf = getattr(p, "leave_from", None)
        lu = getattr(p, "leave_until", None)
        if not lf and not lu:
            return None
        start = lf or lu
        if start is None:
            return None
        if _working_days_until(report_date, start) > within_working_days:
            return None
        start_fmt = start.strftime("%-d %b").replace(" 0", " ")
        if lu and lu != start:
            end_fmt = lu.strftime("%-d %b").replace(" 0", " ")
            return f"out {start_fmt} → {end_fmt}"
        return f"out {start_fmt}"
    return None


def build_leave_badges(digest: dict, roster_rows: list | None, report_date: date) -> dict[str, str]:
    badges: dict[str, str] = {}
    people = {row.get("person") for row in digest.get("status") or [] if row.get("person")}
    for name in people:
        badge = leave_badge_for_person(name, roster_rows, report_date)
        if badge:
            badges[name] = badge
    digest["leave_badges"] = badges
    return badges


# --- C9: silence grouping + streaks ------------------------------------------


def _group_no_report_rows(digest: dict) -> None:
    rows = list(digest.get("no_report") or [])
    silent = [r for r in rows if r.get("status") == "no_report"]
    other = [r for r in rows if r.get("status") != "no_report"]
    if len(silent) <= 1:
        digest["no_report"] = other + silent
        digest["no_report_grouped"] = None
        return
    names = [r.get("name") or "" for r in silent if r.get("name")]
    digest["no_report_grouped"] = {
        "names": names,
        "label": " · ".join(names),
        "context": "No report — no approved leave",
    }
    digest["no_report"] = other


def _fmt_short_date(d: date) -> str:
    return d.strftime("%-d %b").replace(" 0", " ")


def _fmt_from_day(d: date) -> str:
    return d.strftime("%a %-d %b").replace(" 0", " ")


def _person_on_leave(person, report_date: date) -> bool:
    return (
        effective_status(
            getattr(person, "status", "active"),
            getattr(person, "leave_until", None),
            report_date,
            leave_from=getattr(person, "leave_from", None),
        )
        == "on_leave"
    )


def _fmt_day_only(d: date) -> str:
    return str(d.day)


def _compact_on_leave_label(person) -> str:
    lf = getattr(person, "leave_from", None)
    lu = getattr(person, "leave_until", None)
    if lf and lu:
        if lf == lu:
            return f"On leave {_fmt_short_date(lf)}"
        if lf.month == lu.month and lf.year == lu.year:
            return f"On leave {_fmt_day_only(lf)} → {_fmt_short_date(lu)}"
        return f"On leave {_fmt_short_date(lf)} → {_fmt_short_date(lu)}"
    if lu:
        return f"On leave → {_fmt_short_date(lu)}"
    if lf:
        return f"On leave from {_fmt_short_date(lf)}"
    return "On leave"


def _first_name(full_name: str) -> str:
    return (full_name or "").strip().split()[0] if full_name else ""


def _upcoming_leave_fragment(person) -> str:
    """e.g. Vlad (→ 2 Sep), Predrag (1 d), Arturs (1 d)."""
    name = _first_name(getattr(person, "full_name", "") or "")
    lf = getattr(person, "leave_from", None)
    lu = getattr(person, "leave_until", None)
    if not lf:
        return name
    if not lu or lu == lf:
        return f"{name} (1 d)"
    wd = working_days_inclusive(lf, lu)
    if wd == 1:
        return f"{name} (1 d)"
    if wd <= 5:
        return f"{name} ({wd} d)"
    return f"{name} (→ {_fmt_short_date(lu)})"


def _upcoming_person_sort_key(person) -> tuple:
    lf = getattr(person, "leave_from", None)
    lu = getattr(person, "leave_until", None) or lf
    wd = working_days_inclusive(lf, lu) if lf else 0
    return (-wd, (getattr(person, "full_name", "") or "").lower())


def _coverage_escalation_index(digest: dict) -> int | None:
    for i, e in enumerate(digest.get("escalations") or [], start=1):
        if not isinstance(e, dict):
            continue
        if str(e.get("text") or "").startswith("+"):
            continue
        if e.get("affected"):
            return i
        if _escalation_issue_key(e).startswith("leave_coverage|"):
            return i
    return None


def build_out_and_quiet(
    digest: dict,
    roster_rows: list | None,
    report_date: date,
    *,
    within_working_days: int = 7,
) -> list[dict]:
    """Build Out & quiet rows matching the v3 digest layout."""
    rows: list[dict] = []

    if roster_rows:
        # 1 — currently on leave (one row per person)
        on_leave_now = [
            p for p in roster_rows
            if getattr(p, "status", None) != "out" and _person_on_leave(p, report_date)
        ]
        on_leave_now.sort(key=lambda p: (getattr(p, "full_name", "") or "").lower())
        for person in on_leave_now:
            rows.append(
                {
                    "kind": "on_leave",
                    "label": getattr(person, "full_name", "") or "",
                    "detail": _compact_on_leave_label(person),
                    "detail_tone": "leave",
                }
            )

        # 2 — upcoming leave grouped by start date
        upcoming_by_start: dict[date, list] = {}
        for person in roster_rows:
            if getattr(person, "status", None) == "out":
                continue
            if _person_on_leave(person, report_date):
                continue
            lf = getattr(person, "leave_from", None)
            if not lf or lf <= report_date:
                continue
            if _working_days_until(report_date, lf) > within_working_days:
                continue
            upcoming_by_start.setdefault(lf, []).append(person)

        esc_ref = _coverage_escalation_index(digest)
        for start in sorted(upcoming_by_start):
            people = sorted(upcoming_by_start[start], key=_upcoming_person_sort_key)
            fragments = ", ".join(_upcoming_leave_fragment(p) for p in people)
            label = f"From {_fmt_from_day(start)} · {fragments}"
            detail = f"see item {esc_ref}" if esc_ref else f"starts {_fmt_short_date(start)}"
            rows.append(
                {
                    "kind": "upcoming",
                    "label": label,
                    "detail": detail,
                    "detail_tone": "muted",
                }
            )

    # 3 — silent designers (grouped when possible)
    grouped = digest.get("no_report_grouped")
    if grouped:
        rows.append(
            {
                "kind": "silent",
                "label": grouped.get("label") or "",
                "detail": grouped.get("context") or "No report — no approved leave",
                "detail_tone": "muted",
            }
        )
    else:
        for nr in digest.get("no_report") or []:
            if nr.get("status") != "no_report":
                continue
            rows.append(
                {
                    "kind": "silent",
                    "label": nr.get("name") or "",
                    "detail": nr.get("context") or "No report — no approved leave",
                    "detail_tone": "muted",
                }
            )

    digest["out_and_quiet"] = rows
    return rows


def _load_prior_silent_names(session: Session | None, report_date: date) -> set[str]:
    if session is None:
        return set()
    pipeline = session.query(Pipeline).filter_by(key="daily-digest").one_or_none()
    if pipeline is None:
        return set()
    run = (
        session.query(PipelineRun)
        .filter(
            PipelineRun.pipeline_id == pipeline.id,
            PipelineRun.report_date < report_date,
            PipelineRun.status.in_([RunStatus.OK, RunStatus.FLAGGED]),
        )
        .order_by(PipelineRun.report_date.desc())
        .first()
    )
    if run is None:
        return set()
    art = (
        session.query(Artifact)
        .filter_by(run_id=run.id, kind="json")
        .order_by(Artifact.id.desc())
        .first()
    )
    if not art or not art.content:
        return set()
    try:
        prior = json.loads(art.content)
    except json.JSONDecodeError:
        return set()
    return {
        r.get("name")
        for r in (prior.get("no_report") or [])
        if r.get("status") == "no_report" and r.get("name")
    }


def _apply_silence_streaks(
    digest: dict,
    report_date: date,
    session: Session | None,
) -> None:
    grouped = digest.get("no_report_grouped")
    if not grouped:
        return
    prior_silent = _load_prior_silent_names(session, report_date)
    repeat = [n for n in grouped.get("names") or [] if n in prior_silent]
    if not repeat:
        return
    label = f"{len(repeat)} designer{'s' if len(repeat) != 1 else ''} silent 2+ days"
    names = " · ".join(repeat)
    digest.setdefault("escalations", []).append(
        {
            "text": f"{label}: {names}",
            "evidence": f"daily reports {report_date.strftime('%-d %b')}",
            "why_ranked_here": "consecutive missing dailies",
        }
    )


# --- C8: counters from body ----------------------------------------------------


def _recompute_at_a_glance(digest: dict) -> None:
    g = digest.setdefault("at_a_glance", {})
    escalations = [
        e for e in (digest.get("escalations") or [])
        if not str(e.get("text") or "").startswith("+")
    ]
    statuses = digest.get("project_statuses") or {}
    waiting_input = sum(
        1 for s in statuses.values()
        if s in ("waiting on you", "blocked on client")
    )
    silent_count = len((digest.get("no_report_grouped") or {}).get("names") or [])
    if not silent_count:
        silent_count = sum(
            1 for r in (digest.get("no_report") or []) if r.get("status") == "no_report"
        )

    g["need_you"] = len(escalations)
    g["waiting_on_input"] = waiting_input
    g["no_report"] = silent_count
    g["need_review"] = g["need_you"]
    g["blocked"] = waiting_input
    g["escalations"] = len(escalations)


# --- C10: escalation source normalization ------------------------------------


def _normalize_escalation_sources(digest: dict) -> None:
    for e in digest.get("escalations") or []:
        if not isinstance(e, dict):
            continue
        sources: list[str] = []
        _collect_sources(e, sources)
        if sources:
            e["evidence"] = _join_sources(sources)
        elif e.get("evidence"):
            e["evidence"] = _join_sources(_split_sources(e["evidence"]))
        e.pop("agent_note", None)
        text = e.get("text") or ""
        if _COVERAGE_PHRASE in text.lower():
            e["text"] = re.sub(
                rf"\s*{_re_escape_coverage()}.*",
                "",
                text,
                flags=re.I,
            ).strip() or text


def _re_escape_coverage() -> str:
    return re.escape(_COVERAGE_PHRASE)


def validate_project_status(status: str | None) -> str | None:
    if status and status.lower() in ALLOWED_PROJECT_STATUSES:
        return status.lower()
    return None
