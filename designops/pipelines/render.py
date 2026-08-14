"""Render the structured digest JSON to the locked HTML (§6.4).

Rendering stays in code so the layout cannot drift — the model returns data, never
markup. Pure and dependency-light (Jinja only), so it is unit-testable on DOM
structure without an LLM or DB.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from designops.pipelines.digest_postprocess import validate_project_status

_TEMPLATES = Path(__file__).resolve().parent.parent / "skills" / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "j2"]),
)

LOAD_DAY_HOURS = 8.0
LOAD_LOW_HOURS = 7.0


def _lines_for_row(row: dict, field: str) -> list[str]:
    lines_key = f"{field}_lines"
    if row.get(lines_key):
        return list(row[lines_key])
    text = (row.get(field) or row.get("line") if field == "done" else row.get(field)) or ""
    text = str(text).strip()
    return [text] if text else []


def _first_name(full_name: str) -> str:
    return (full_name or "").strip().split()[0] if full_name else ""


def _escalation_names(digest: dict) -> set[str]:
    names: set[str] = set()
    for e in digest.get("escalations") or []:
        if not isinstance(e, dict) or str(e.get("text") or "").startswith("+"):
            continue
        who = (e.get("who") or "").strip()
        if who:
            names.add(who)
        for a in e.get("affected") or []:
            if isinstance(a, dict) and a.get("who"):
                names.add(str(a["who"]).strip())
    return names


def _out_chip(leave_badge: str | None) -> dict | None:
    if not leave_badge:
        return None
    label = leave_badge.strip()
    if label.lower().startswith("out "):
        label = "Out " + label[4:]
    elif not label.lower().startswith("out"):
        label = label[:1].upper() + label[1:] if label else label
    return {"kind": "out", "label": label}


def _note_lab(note: dict) -> str:
    ntype = (note.get("type") or "").lower()
    text = (note.get("text") or "").lower()
    if "cover" in text or "coverage" in text:
        return "Cover"
    if ntype == "waiting":
        return "Waiting"
    if ntype == "stalled":
        return "Stalled"
    return "Context"


def _note_kind(lab: str) -> str:
    if lab == "Cover" or lab == "Waiting":
        return "w"
    if lab == "Stalled":
        return "r"
    return ""


def build_person_groups(digest: dict, report_date: date) -> list[dict]:
    """One card per designer; projects nest inside. Attention-needed first."""
    status = list(digest.get("status") or [])
    project_notes_map = digest.get("project_notes") or {}
    project_statuses = digest.get("project_statuses") or {}
    leave_badges = digest.get("leave_badges") or {}
    hours_by_person = digest.get("hours_by_person") or {}
    jira_ok = bool(digest.get("jira_available"))
    needs_olga = _escalation_names(digest)
    on_leave_names = {
        r.get("name")
        for r in (digest.get("no_report") or [])
        if r.get("status") == "on_leave" and r.get("name")
    }

    cards: dict[str, dict] = {}
    order: list[str] = []
    for row in status:
        person = (row.get("person") or "").strip() or "Unknown"
        key = person.lower()
        if key not in cards:
            cards[key] = {
                "name": person,
                "projects": [],
                "_seen_proj": set(),
                "max_streak": 0,
                "waiting": False,
                "blocked": False,
                "needs_olga": person in needs_olga,
            }
            order.append(key)
        card = cards[key]
        proj = (row.get("project") or "").strip() or "Unassigned"
        pk = proj.lower()
        pstatus = validate_project_status(project_statuses.get(pk))
        if pstatus == "waiting on you":
            card["waiting"] = True
        if pstatus == "blocked on client":
            card["blocked"] = True
        if row.get("_waiting_on_olga"):
            card["waiting"] = True
        repeat = row.get("repeat")
        if isinstance(repeat, dict):
            card["max_streak"] = max(card["max_streak"], int(repeat.get("streak") or 0))

        notes_raw = list(project_notes_map.get(pk) or [])
        has_repeat = bool(repeat)
        ctx_notes = []
        wait_rows = []
        for n in notes_raw:
            lab = _note_lab(n)
            if has_repeat and lab == "Stalled":
                continue
            entry = {
                "lab": lab if lab != "Waiting" else "Waiting",
                "kind": _note_kind(lab),
                "text": n.get("text") or "",
                "sources": list(n.get("sources") or []),
            }
            if lab == "Waiting" and "cover" not in (n.get("text") or "").lower():
                wait_rows.append(entry)
            else:
                ctx_notes.append(entry)

        proj_block = {
            "name": proj,
            "untracked": bool(row.get("untracked")),
            "done_lines": _lines_for_row(row, "done"),
            "next_lines": _lines_for_row(row, "next"),
            "tickets": list(row.get("tickets") or []) if jira_ok else [],
            "ticket_hours": row.get("ticket_hours") if jira_ok else None,
            "ticket_hours_label": (
                f"{float(row.get('ticket_hours') or 0):.1f}h" if jira_ok else None
            ),
            "no_ticket_found": bool(row.get("no_ticket_found")) if jira_ok else False,
            "no_time_logged": row.get("no_time_logged") if jira_ok else None,
            "repeat": repeat,
            "wait_rows": wait_rows,
            "notes": ctx_notes,
            "show_jira": jira_ok,
        }
        if pk in card["_seen_proj"]:
            for existing in card["projects"]:
                if existing["name"].lower() == pk:
                    if proj_block["done_lines"] and not existing["done_lines"]:
                        existing["done_lines"] = proj_block["done_lines"]
                    if proj_block["next_lines"] and not existing["next_lines"]:
                        existing["next_lines"] = proj_block["next_lines"]
                    if proj_block["tickets"] and not existing["tickets"]:
                        existing["tickets"] = proj_block["tickets"]
                    if proj_block["repeat"] and not existing["repeat"]:
                        existing["repeat"] = proj_block["repeat"]
                    break
            continue
        card["_seen_proj"].add(pk)
        card["projects"].append(proj_block)

    person_groups: list[dict] = []
    for key in order:
        card = cards[key]
        card.pop("_seen_proj", None)
        person = card["name"]
        chips: list[dict] = []
        out = _out_chip(leave_badges.get(person))
        if out:
            chips.append(out)
        if person in on_leave_names:
            chips.append({"kind": "leave", "label": "On leave"})
        if card["waiting"]:
            chips.append({"kind": "wait", "label": "Waiting on you"})
        if card["blocked"]:
            chips.append({"kind": "blk", "label": "Blocked"})
        if card["max_streak"] >= 3:
            chips.append({"kind": "rep", "label": f"Repeating ×{card['max_streak']}d"})
        hours = hours_by_person.get(person)
        if hours is None and jira_ok:
            hours = 0.0
            for p in card["projects"]:
                hours += float(p.get("ticket_hours") or 0)
            hours = round(hours, 2)
        bar_pct = 0
        bar_class = "zero"
        if jira_ok and hours is not None:
            bar_pct = int(min(100, round((hours / LOAD_DAY_HOURS) * 100)))
            if hours <= 0:
                bar_class = "zero"
            elif hours < LOAD_LOW_HOURS:
                bar_class = "low"
            else:
                bar_class = "ok"
        n_proj = len(card["projects"])
        proj_label = "1 project" if n_proj == 1 else f"{n_proj} projects"
        attention = bool(
            card["needs_olga"] or card["max_streak"] >= 3 or card["waiting"] or card["blocked"]
        )
        person_groups.append(
            {
                "name": person,
                "chips": chips,
                "hours": hours,
                "hours_label": f"{hours:.1f}h" if hours is not None and jira_ok else None,
                "bar_pct": bar_pct,
                "bar_class": bar_class,
                "project_count_label": proj_label,
                "show_load": jira_ok,
                "attention": attention,
                "needs_olga": card["needs_olga"],
                "max_streak": card["max_streak"],
                "projects": card["projects"],
            }
        )

    person_groups.sort(
        key=lambda c: (
            0 if (c["needs_olga"] or c["max_streak"] >= 3) else 1,
            0 if c["attention"] else 1,
            c["name"].lower(),
        )
    )
    return person_groups


def build_project_index(digest: dict, person_groups: list[dict]) -> list[dict]:
    """One-line index so shared-project work stays legible after the person split."""
    project_statuses = digest.get("project_statuses") or {}
    by_proj: dict[str, dict] = {}
    order: list[str] = []
    for card in person_groups:
        for p in card["projects"]:
            name = p["name"]
            key = name.lower()
            if key not in by_proj:
                by_proj[key] = {
                    "project": name,
                    "people": [],
                    "status_tag": validate_project_status(project_statuses.get(key)),
                }
                order.append(key)
            if card["name"] not in by_proj[key]["people"]:
                by_proj[key]["people"].append(card["name"])
    index = []
    for key in order:
        g = by_proj[key]
        firsts = [_first_name(n) for n in g["people"]]
        g["people_label"] = ", ".join(firsts)
        index.append(g)
    return index


def render_digest(
    digest: dict,
    report_date: date,
    *,
    sample: bool = True,
    coverage: dict | None = None,
) -> str:
    template = _env.get_template("digest.html.j2")
    person_groups = build_person_groups(digest, report_date)
    project_index = build_project_index(digest, person_groups)

    plans = list(digest.get("todays_plans") or [])
    leave_plans = [p for p in plans if p.get("leave_upcoming")]
    other_plans = [
        p for p in plans if not p.get("leave_upcoming") and (p.get("plan") or "").strip()
    ]

    no_report = list(digest.get("no_report") or [])
    on_leave = [r for r in no_report if r.get("status") == "on_leave"]
    on_leave_names = {r.get("name") for r in on_leave if r.get("name")}
    no_report_grouped = digest.get("no_report_grouped")
    out_and_quiet = digest.get("out_and_quiet") or []

    return template.render(
        digest=digest,
        person_groups=person_groups,
        project_index=project_index,
        leave_plans=leave_plans,
        other_plans=other_plans,
        on_leave=on_leave,
        on_leave_names=on_leave_names,
        no_daily=[r for r in no_report if r.get("status") != "on_leave"],
        no_report_grouped=no_report_grouped,
        out_and_quiet=out_and_quiet,
        report_date_label=report_date.strftime("%A, %-d %b %Y"),
        sample=sample,
        coverage=coverage or {},
        jira_worklog_date=report_date.strftime("%-d %b"),
    )


def render_weekly_backlog(
    digest: dict,
    week_monday: date,
    friday_date: date,
    *,
    sample: bool = True,
    coverage: dict | None = None,
) -> str:
    template = _env.get_template("weekly_backlog.html.j2")
    return template.render(
        digest=digest,
        week_label=week_monday.strftime("%-d %b %Y"),
        friday_label=friday_date.strftime("%A, %-d %b %Y"),
        sample=sample,
        coverage=coverage or {},
    )


def render_weekly_health(
    digest: dict,
    as_of: date,
    *,
    sample: bool = True,
    coverage: dict | None = None,
) -> str:
    template = _env.get_template("weekly_health.html.j2")
    return template.render(
        digest=digest,
        as_of_label=as_of.strftime("%a %-d %b %Y"),
        sample=sample,
        coverage=coverage or {},
    )
