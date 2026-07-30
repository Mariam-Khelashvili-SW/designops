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
    return template.render(
        digest=digest,
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
