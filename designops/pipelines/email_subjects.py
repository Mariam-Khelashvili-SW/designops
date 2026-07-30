"""Email subject lines for pipeline digests (pipeline delivery + run-page send)."""

from __future__ import annotations

from datetime import date

from designops.pipelines.weekly_availability import resolve_week_monday


def _fmt(d: date) -> str:
    # e.g. 31 Jul 2026 — matches report body labels
    return d.strftime("%-d %b %Y") if hasattr(d, "strftime") else d.isoformat()


def email_subject_for_pipeline(pipeline_key: str, report_date: date) -> str:
    """Subject for a run of the given pipeline on report_date."""
    key = (pipeline_key or "").strip()
    if key == "weekly-health":
        # Snapshot report: dated the day it was generated, e.g. "Tue 28 Jul 2026".
        return f"Design Weekly Health & Budget — {report_date.strftime('%a %-d %b %Y')}"
    if key == "weekly-backlog":
        mon = resolve_week_monday(report_date)
        return f"Design Weekly Planning Board — week of {_fmt(mon)}"
    return f"Design Daily Digest — {_fmt(report_date)}"
