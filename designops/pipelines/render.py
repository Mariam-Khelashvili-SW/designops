"""Render the structured digest JSON to the locked HTML (§6.4).

Rendering stays in code so the layout cannot drift — the model returns data, never
markup. Pure and dependency-light (Jinja only), so it is unit-testable on DOM
structure without an LLM or DB.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = Path(__file__).resolve().parent.parent / "skills" / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(["html", "j2"]),
)


def render_digest(
    digest: dict,
    report_date: date,
    *,
    sample: bool = True,
    coverage: dict | None = None,
) -> str:
    template = _env.get_template("digest.html.j2")
    status = list(digest.get("status") or [])
    # Group by project, then people under each (Done / Next).
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
        done = (row.get("done") or row.get("line") or "").strip()
        nxt = (row.get("next") or "").strip()
        untracked = bool(row.get("untracked"))
        # Merge duplicate person×project rows (keep first done/next, OR later text).
        key = person.lower()
        if key in g["_seen"]:
            for existing in g["people"]:
                if (existing.get("person") or "").lower() == key:
                    if done and not (existing.get("done") or "").strip():
                        existing["done"] = done
                    if nxt and not (existing.get("next") or "").strip():
                        existing["next"] = nxt
                    note = (row.get("agent_note") or "").strip()
                    if note and not (existing.get("agent_note") or "").strip():
                        existing["agent_note"] = note
                    existing["untracked"] = existing.get("untracked") or untracked
                    break
            continue
        g["_seen"].add(key)
        g["people"].append(
            {
                "person": person,
                "done": done,
                "next": nxt,
                "untracked": untracked,
                "agent_note": (row.get("agent_note") or "").strip() or None,
            }
        )
    for g in project_groups:
        g.pop("_seen", None)
        g["untracked"] = any(p.get("untracked") for p in g["people"])
        g["heads_ups"] = []

    # Nest heads-ups under the matching project (not a top-level section).
    for h in digest.get("heads_ups") or []:
        if not isinstance(h, dict):
            continue
        text = (h.get("text") or "").strip()
        if not text:
            continue
        row = {
            "text": text,
            "evidence": (h.get("evidence") or "").strip() or None,
            "who": (h.get("who") or "").strip() or None,
            "project": (h.get("project") or "").strip() or None,
        }
        proj = (row["project"] or "").strip() or "Unassigned"
        target = by_project.get(proj)
        if target is None:
            for name, g in by_project.items():
                if name.lower() == proj.lower():
                    target = g
                    break
        if target is None:
            target = {
                "project": proj,
                "people": [],
                "untracked": False,
                "heads_ups": [],
            }
            by_project[proj] = target
            project_groups.append(target)
        target["heads_ups"].append(row)

    plans = list(digest.get("todays_plans") or [])
    # Main body uses status[].next; only show leftover plans (e.g. upcoming leave).
    leave_plans = [p for p in plans if p.get("leave_upcoming")]
    other_plans = [
        p for p in plans if not p.get("leave_upcoming") and (p.get("plan") or "").strip()
    ]

    no_report = list(digest.get("no_report") or [])
    on_leave = [r for r in no_report if r.get("status") == "on_leave"]
    on_leave_names = {r.get("name") for r in on_leave if r.get("name")}
    return template.render(
        digest=digest,
        project_groups=project_groups,
        leave_plans=leave_plans,
        other_plans=other_plans,
        on_leave=on_leave,
        on_leave_names=on_leave_names,
        no_daily=[r for r in no_report if r.get("status") != "on_leave"],
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
