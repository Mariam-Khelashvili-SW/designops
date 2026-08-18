"""Human-readable reasons for a pipeline run's status pill.

`Needs a look` (flagged) is set when coverage is thin or an input failed.
The pill alone does not say which — these helpers spell it out from `run.counts`.
"""

from __future__ import annotations

from typing import Any


def _counts(run: Any) -> dict:
    counts = getattr(run, "counts", None)
    return counts if isinstance(counts, dict) else {}


def _coverage(run: Any) -> dict:
    cov = _counts(run).get("coverage")
    return cov if isinstance(cov, dict) else {}


def explain_run_status(
    run: Any | None,
    *,
    min_coverage: float | None = None,
) -> list[str]:
    """Why this run is flagged or failed. Empty when the run is clean or missing."""
    if run is None:
        return []
    st = (getattr(run, "status", None) or "").lower()
    if st == "failed":
        err = (getattr(run, "error", None) or "").strip()
        return [err] if err else ["The run failed before a digest was produced."]
    if st != "flagged":
        return []

    counts = _counts(run)
    cov = _coverage(run)
    reasons: list[str] = []

    failed = int(cov.get("exports_failed") or 0)
    if failed:
        reasons.append(f"{failed} Fairwind export{'s' if failed != 1 else ''} failed")

    fri_failed = int(cov.get("friday_exports_failed") or 0)
    if fri_failed:
        reasons.append(
            f"{fri_failed} Friday Fairwind export{'s' if fri_failed != 1 else ''} failed"
        )

    if cov.get("jira_incomplete"):
        reasons.append("Jira data was incomplete or unreachable")
    if cov.get("invoice_incomplete"):
        reasons.append("Fairwind invoice data was incomplete")

    specific = bool(
        failed
        or fri_failed
        or cov.get("jira_incomplete")
        or cov.get("invoice_incomplete")
    )
    if cov.get("incomplete") and not specific:
        reasons.append("Some report inputs were incomplete")

    if min_coverage is None:
        from designops.core.config import get_settings

        floor = get_settings().min_coverage
    else:
        floor = min_coverage
    ratio = counts.get("coverage_ratio")
    if isinstance(ratio, (int, float)) and ratio < floor:
        reported = counts.get("reported")
        silent = counts.get("silent")
        extra = ""
        if reported is not None and silent is not None:
            extra = f" ({reported} reported, {silent} silent)"
        reasons.append(
            f"Roster coverage {ratio:.0%} is below the {floor:.0%} floor{extra}"
        )

    unmatched = int(counts.get("unmatched_projects") or 0)
    if unmatched:
        reasons.append(
            f"{unmatched} project name{'s' if unmatched != 1 else ''} not mapped to an account"
        )

    if not reasons:
        reasons.append(
            "Automatic checks found something to confirm — open the run for details"
        )
    return reasons


def status_why(run: Any | None, *, min_coverage: float | None = None) -> str:
    """Single sentence (or two) for templates."""
    parts = explain_run_status(run, min_coverage=min_coverage)
    if not parts:
        return ""
    return " ".join(p.rstrip(".") + "." for p in parts)
