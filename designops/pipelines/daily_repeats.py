"""Persist Next lines and flag the same plan repeating 3+ reporting days.

History lives in ``daily_next_snapshot``. Artifact JSON is a read fallback for
runs before the table existed. No history → say so; never guess a streak.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from designops.core.enums import RunStatus
from designops.core.identity import effective_status
from designops.core.models import Artifact, DailyNextSnapshot, Pipeline, PipelineRun

REPEAT_THRESHOLD = 3
WATCH_THRESHOLD = 2
HISTORY_DAYS = 8

_FILLER_RE = re.compile(
    r"\b("
    r"try to|trying to|continue to|continue|continuing|"
    r"will|gonna|need to|plan to|planning to|"
    r"start to|started to|aim to|hoping to|please"
    r")\b",
    re.I,
)
_NO_PROGRESS_RE = re.compile(
    r"\bno(?:\s+design)?\s+progress\b|\bno progress reported\b",
    re.I,
)
_PUNCT_RE = re.compile(r"[^\w\s&]")


def normalize_intent(text: str) -> str:
    """Normalise a Next/Done clause for streak matching (filler stripped)."""
    t = (text or "").strip().lower()
    if not t:
        return ""
    t = _FILLER_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def split_clauses(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[;\n]+", text or "") if p.strip()]
    return parts or ([text.strip()] if (text or "").strip() else [])


def intents_for_row(done: str, nxt: str) -> list[tuple[str, str]]:
    """(display_clause, intent_key) pairs to track for this person×project."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for clause in split_clauses(nxt):
        key = normalize_intent(clause)
        if key and key not in seen:
            seen.add(key)
            out.append((clause.strip(), key))
    if _NO_PROGRESS_RE.search(done or ""):
        key = normalize_intent("no progress reported")
        if key not in seen:
            out.append(("no progress reported", key))
    return out


def intents_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 12 and shorter in longer:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return overlap >= 0.75 and min(len(ta), len(tb)) >= 3


def _fmt_day(d: date) -> str:
    return d.strftime("%-d %b").replace(" 0", " ")


def _person_on_leave(person, day: date) -> bool:
    """True when `day` falls inside the person's leave window (skip, don't break streak)."""
    if person is None:
        return False
    lf = getattr(person, "leave_from", None)
    lu = getattr(person, "leave_until", None)
    if lf and lu:
        return lf <= day <= lu
    if lu and not lf:
        return day <= lu
    if lf and not lu:
        return day >= lf
    return (
        effective_status(
            getattr(person, "status", "active"),
            lu,
            day,
            leave_from=lf,
        )
        == "on_leave"
    )


def load_next_history(
    session: Session | None,
    report_date: date,
    *,
    limit_days: int = HISTORY_DAYS,
) -> list[dict]:
    """Prior reporting-day snapshots, newest first. Artifacts fill gaps."""
    if session is None:
        return []
    cutoff = report_date - timedelta(days=max(limit_days * 2, 14))
    rows = (
        session.query(DailyNextSnapshot)
        .filter(
            DailyNextSnapshot.report_date < report_date,
            DailyNextSnapshot.report_date >= cutoff,
        )
        .order_by(DailyNextSnapshot.report_date.desc())
        .all()
    )
    out: list[dict] = []
    seen_dates: set[date] = set()
    for r in rows:
        seen_dates.add(r.report_date)
        out.append(
            {
                "report_date": r.report_date,
                "person": r.person_name,
                "project": r.project,
                "next": r.next_text or "",
                "done": r.done_text or "",
                "intent_key": r.intent_key or "",
                "hours_logged": r.hours_logged,
            }
        )
    pipeline = session.query(Pipeline).filter_by(key="daily-digest").one_or_none()
    if pipeline is None:
        return out
    runs = (
        session.query(PipelineRun)
        .filter(
            PipelineRun.pipeline_id == pipeline.id,
            PipelineRun.report_date < report_date,
            PipelineRun.report_date >= cutoff,
            PipelineRun.status.in_([RunStatus.OK, RunStatus.FLAGGED]),
        )
        .order_by(PipelineRun.report_date.desc())
        .limit(limit_days)
        .all()
    )
    for run in runs:
        if run.report_date in seen_dates:
            continue
        art = (
            session.query(Artifact)
            .filter_by(run_id=run.id, kind="json")
            .order_by(Artifact.id.desc())
            .first()
        )
        if art is None or not art.content:
            continue
        try:
            digest = json.loads(art.content)
        except json.JSONDecodeError:
            continue
        seen_dates.add(run.report_date)
        for s in digest.get("status") or []:
            nxt = (s.get("next") or "").strip()
            done = (s.get("done") or "").strip()
            if not nxt and not done:
                continue
            out.append(
                {
                    "report_date": run.report_date,
                    "person": s.get("person") or "",
                    "project": s.get("project") or "",
                    "next": nxt,
                    "done": done,
                    "intent_key": "",
                    "hours_logged": s.get("ticket_hours"),
                }
            )
    return out


def persist_next_snapshots(
    session: Session | None,
    run: PipelineRun | None,
    digest: dict,
    report_date: date,
    roster_rows: list | None = None,
) -> int:
    """Replace this report_date's snapshots with the current status rows."""
    if session is None:
        return 0
    session.query(DailyNextSnapshot).filter_by(report_date=report_date).delete(
        synchronize_session=False
    )
    by_name = {
        getattr(p, "full_name", ""): p
        for p in (roster_rows or [])
        if getattr(p, "full_name", None)
    }
    n = 0
    hours_map = digest.get("hours_by_person_project") or {}
    for s in digest.get("status") or []:
        person = (s.get("person") or "").strip()
        project = (s.get("project") or "").strip()
        nxt = (s.get("next") or "").strip()
        done = (s.get("done") or "").strip()
        if not person or not project:
            continue
        if not nxt and not done:
            continue
        intents = intents_for_row(done, nxt)
        intent_key = intents[0][1] if intents else normalize_intent(nxt or done)
        hours = s.get("ticket_hours")
        if hours is None:
            hours = hours_map.get(f"{person}|{project}")
        row_person = by_name.get(person)
        session.add(
            DailyNextSnapshot(
                run_id=getattr(run, "id", None),
                report_date=report_date,
                person_id=getattr(row_person, "id", None),
                person_name=person,
                project=project,
                next_text=nxt,
                done_text=done,
                intent_key=intent_key,
                hours_logged=float(hours) if hours is not None else None,
            )
        )
        n += 1
    session.flush()
    return n


def prior_hours_by_person_project(
    history: list[dict],
) -> dict[tuple[str, str], list[tuple[date, float]]]:
    out: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for h in history:
        name = (h.get("person") or "").strip()
        proj = (h.get("project") or "").strip().lower()
        when = h.get("report_date")
        hrs = h.get("hours_logged")
        if not name or not proj or not when or hrs is None:
            continue
        out.setdefault((name, proj), []).append((when, float(hrs)))
    return out


def detect_repeats(
    digest: dict,
    *,
    report_date: date,
    history: list[dict] | None,
    roster_rows: list | None = None,
) -> None:
    """Attach Repeat rows. No usable history → unavailable note, no guessed streaks."""
    digest["repeat_check"] = "ok"
    digest["repeating_people"] = []
    digest["repeat_flags"] = []

    if history is None:
        digest["repeat_check"] = "unavailable"
        digest["repeat_note"] = "repeat check unavailable (no history)"
        return

    hist_dates = {h["report_date"] for h in history if h.get("report_date")}
    if not hist_dates:
        digest["repeat_check"] = "unavailable"
        digest["repeat_note"] = "repeat check unavailable (no history)"
        return

    by_name = {
        getattr(p, "full_name", ""): p
        for p in (roster_rows or [])
        if getattr(p, "full_name", None)
    }
    waiting_projects = {
        (s.get("project") or "").strip().lower()
        for s in digest.get("status") or []
        if s.get("_waiting_on_olga")
    }
    for e in digest.get("escalations") or []:
        text = (e.get("text") or "").lower()
        if "waiting on your" in text or "review" in text:
            if e.get("project"):
                waiting_projects.add(str(e["project"]).strip().lower())

    indexed: dict[tuple[str, str], dict[date, list[dict]]] = {}
    for h in history:
        person = (h.get("person") or "").strip()
        project = (h.get("project") or "").strip()
        when = h.get("report_date")
        if not person or not project or not when:
            continue
        indexed.setdefault((person.lower(), project.lower()), {}).setdefault(
            when, []
        ).append(h)

    flags: list[dict] = []
    for row in digest.get("status") or []:
        person = (row.get("person") or "").strip()
        project = (row.get("project") or "").strip()
        done = (row.get("done") or "").strip()
        nxt = (row.get("next") or "").strip()
        if not person or not project:
            continue
        current = intents_for_row(done, nxt)
        if not current:
            row.pop("repeat", None)
            continue
        hist_for = indexed.get((person.lower(), project.lower()), {})
        best: dict | None = None
        for clause, intent in current:
            streak, days = _streak_for_intent(
                intent,
                hist_for,
                report_date,
                person_obj=by_name.get(person),
            )
            if streak < WATCH_THRESHOLD:
                continue
            behind_olga = project.lower() in waiting_projects or bool(
                row.get("_waiting_on_olga")
            )
            if streak < REPEAT_THRESHOLD and not behind_olga:
                continue
            cand = {
                "person": person,
                "project": project,
                "clause": clause,
                "intent": intent,
                "days": days,
                "streak": streak,
            }
            if best is None or cand["streak"] > best["streak"]:
                best = cand
        if best is None:
            row.pop("repeat", None)
            continue
        tickets = row.get("tickets") or []
        hours = float(row.get("ticket_hours") or 0)
        parts = _repeat_parts(best, tickets, hours, report_date)
        row["repeat"] = {
            **parts,
            "streak": best["streak"],
            "clause": best["clause"],
            "days": [d.isoformat() for d in best["days"]],
            "shared": False,
        }
        flags.append(best)

    by_proj_intent: dict[tuple[str, str], list[dict]] = {}
    for f in flags:
        if f["streak"] < REPEAT_THRESHOLD:
            continue
        by_proj_intent.setdefault((f["project"].lower(), f["intent"]), []).append(f)
    shared_people: dict[tuple[str, str], str] = {}
    for (_pk, _intent), group in by_proj_intent.items():
        names = sorted({g["person"] for g in group})
        if len(names) < 2:
            continue
        firsts = " and ".join(n.split()[0] for n in names)
        clause = group[0]["clause"]
        streak = group[0]["streak"]
        extra = (
            f"{firsts} have carried the identical next step — “{clause}” — "
            f"for {streak} days each. Same plan, {len(names)} people: "
            f"worth checking the split is real."
        )
        for g in group:
            shared_people[(g["person"], g["project"])] = extra

    for row in digest.get("status") or []:
        key = ((row.get("person") or "").strip(), (row.get("project") or "").strip())
        extra = shared_people.get(key)
        if extra and row.get("repeat"):
            row["repeat"]["text"] = extra
            row["repeat"]["shared"] = True
            row["repeat"]["tail"] = extra

    digest["repeating_people"] = sorted(
        {f["person"] for f in flags if f["streak"] >= REPEAT_THRESHOLD}
    )
    digest["repeat_flags"] = flags


def _streak_for_intent(
    intent: str,
    hist_by_date: dict[date, list[dict]],
    report_date: date,
    *,
    person_obj: Any = None,
) -> tuple[int, list[date]]:
    """Consecutive reporting days (today + matching prior), skipping weekends/leave."""
    days = [report_date]
    streak = 1
    cursor = report_date - timedelta(days=1)
    scanned = 0
    while scanned < HISTORY_DAYS * 2:
        if cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
            continue
        if _person_on_leave(person_obj, cursor):
            cursor -= timedelta(days=1)
            continue
        scanned += 1
        rows = hist_by_date.get(cursor) or []
        if not rows:
            break
        matched = False
        for h in rows:
            keys = []
            stored = (h.get("intent_key") or "").strip()
            if stored:
                keys.append(stored)
            keys.extend(
                k for _, k in intents_for_row(h.get("done") or "", h.get("next") or "")
            )
            if any(intents_match(intent, k) for k in keys if k):
                matched = True
                break
        if not matched:
            break
        streak += 1
        days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return streak, days


def _repeat_parts(
    flag: dict, tickets: list[dict], hours: float, report_date: date
) -> dict:
    clause = flag["clause"]
    days: list[date] = flag["days"]
    streak = flag["streak"]
    if len(days) >= 2:
        day_list = ", ".join(_fmt_day(d) for d in days[:-1]) + f" and {_fmt_day(days[-1])}"
    else:
        day_list = _fmt_day(report_date)
    ticket_bits = []
    for t in tickets:
        key = t.get("key") or ""
        status = t.get("status") or ""
        stale = t.get("stale_days")
        spent = t.get("spent_hours")
        orig = t.get("original_hours")
        if key and status:
            bit = f"{key} still {status}"
            if orig and spent and float(spent) > float(orig):
                ticket_bits.append(
                    f"{bit}, {spent:g}h logged against a {orig:g}h estimate"
                )
            else:
                ticket_bits.append(bit)
        if stale and stale >= 3 and key:
            ticket_bits.append(f"{key} hasn't moved in {stale} days")
    if hours <= 0:
        tail = ", no progress and no time logged on any of them"
        if ticket_bits:
            tail += (
                f". {ticket_bits[-1]}. Either the follow-up isn't happening or the "
                "client isn't answering — worth a direct ask."
            )
        else:
            tail += "."
    elif ticket_bits:
        tail = f". {ticket_bits[0]}."
    else:
        tail = f", {hours:g}h logged in total."
    text = f"“{clause}” has been the plan on {day_list} — {streak} days{tail}"
    return {
        "clause": clause,
        "day_list": day_list,
        "streak": streak,
        "tail": tail,
        "text": text,
    }


def apply_repeat_kpis(digest: dict) -> None:
    g = digest.setdefault("at_a_glance", {})
    g["repeating"] = len(digest.get("repeating_people") or [])
