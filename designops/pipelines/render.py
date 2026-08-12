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


def _lines_for_row(row: dict, field: str) -> list[str]:
    lines_key = f"{field}_lines"
    if row.get(lines_key):
        return list(row[lines_key])
    text = (row.get(field) or row.get("line") if field == "done" else row.get(field)) or ""
    text = str(text).strip()
    return [text] if text else []


def render_digest(
    digest: dict,
    report_date: date,
    *,
    sample: bool = True,
    coverage: dict | None = None,
) -> str:
    template = _env.get_template("digest.html.j2")
    status = list(digest.get("status") or [])
    project_notes_map = digest.get("project_notes") or {}
    project_statuses = digest.get("project_statuses") or {}
    leave_badges = digest.get("leave_badges") or {}

    project_groups: list[dict] = []
    by_project: dict[str, dict] = {}
    for row in status:
        proj = (row.get("project") or "").strip() or "Unassigned"
        if proj not in by_project:
            g = {"project": proj, "people": [], "_seen": set()}
            by_project[proj] = g
            project_groups.append(g)
        g = by_project[proj]
        person = (row.get("person") or "").strip() or "Unknown"
        done_lines = _lines_for_row(row, "done")
        next_lines = _lines_for_row(row, "next")
        untracked = bool(row.get("untracked"))
        key = person.lower()
        if key in g["_seen"]:
            for existing in g["people"]:
                if (existing.get("person") or "").lower() == key:
                    if done_lines and not existing.get("done_lines"):
                        existing["done_lines"] = done_lines
                    if next_lines and not existing.get("next_lines"):
                        existing["next_lines"] = next_lines
                    existing["untracked"] = existing.get("untracked") or untracked
                    break
            continue
        g["_seen"].add(key)
        g["people"].append(
            {
                "person": person,
                "done_lines": done_lines,
                "next_lines": next_lines,
                "leave_badge": leave_badges.get(person),
                "untracked": untracked,
            }
        )

    for g in project_groups:
        g.pop("_seen", None)
        g["untracked"] = any(p.get("untracked") for p in g["people"])
        proj_key = g["project"].lower()
        raw_status = project_statuses.get(proj_key)
        g["status_tag"] = validate_project_status(raw_status)
        notes = project_notes_map.get(proj_key) or []
        g["notes"] = notes

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
        project_groups=project_groups,
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
