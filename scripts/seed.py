"""Seed roster + project registry from seeds/*.yaml, and pipeline rows (A1 + A2 + A3).

Idempotent: re-running upserts by natural key (person = first email or full_name;
project = canonical_name; pipeline = key). Local edits made in the UI are preserved
for fields the seed does not own. Run: `python -m scripts.seed`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from designops.core.bootstrap import ensure_pipelines
from designops.core.db import session_scope
from designops.core.models import Person, Project

SEEDS = Path(__file__).resolve().parent.parent / "designops" / "seeds"


def _load(name: str) -> dict:
    with open(SEEDS / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_people(session) -> tuple[int, int]:
    data = _load("roster.yaml")
    created = updated = 0
    for row in data.get("people", []):
        emails = row.get("emails") or []
        # natural key: first email if present, else full_name (unresolved rows)
        existing = None
        if emails:
            for p in session.query(Person).all():
                if set(e.lower() for e in p.emails) & {e.lower() for e in emails}:
                    existing = p
                    break
        if existing is None:
            existing = session.query(Person).filter_by(full_name=row["full_name"]).one_or_none()

        if existing is None:
            session.add(
                Person(
                    full_name=row["full_name"],
                    emails=[e.lower() for e in emails],
                    jira_account_id=row.get("jira_account_id"),
                    display_aliases=row.get("display_aliases") or [],
                    role=row.get("role"),
                    status=row.get("status", "active"),
                    squad=row.get("squad"),
                    identity_verified=row.get("identity_verified", False),
                    notes=row.get("notes"),
                )
            )
            created += 1
        else:
            existing.full_name = row["full_name"]
            existing.emails = [e.lower() for e in emails]
            existing.jira_account_id = row.get("jira_account_id")
            existing.display_aliases = row.get("display_aliases") or []
            existing.role = row.get("role")
            existing.status = row.get("status", "active")
            existing.squad = row.get("squad")
            existing.identity_verified = row.get("identity_verified", False)
            existing.notes = row.get("notes")
            updated += 1
    return created, updated


def seed_projects(session) -> tuple[int, int]:
    data = _load("projects.yaml")
    created = updated = 0
    for row in data.get("projects", []):
        existing = (
            session.query(Project).filter_by(canonical_name=row["canonical_name"]).one_or_none()
        )
        fields = dict(
            canonical_name=row["canonical_name"],
            aliases=row.get("aliases") or [],
            jira_project_key=row.get("jira_project_key"),
            fairwind_account_id=row.get("fairwind_account_id"),
            active=row.get("active", True),
            track_daily=row.get("track_daily", False),
            track_weekly_health=row.get("track_weekly_health", False),
            # Display/commercial fields are live at generate time; seeds clear them.
            display_subtitle=row.get("display_subtitle"),
            signed_design_estimate_h=row.get("signed_design_estimate_h"),
            estimate_basis=row.get("estimate_basis"),
            agreement_summary=row.get("agreement_summary") or {},
            jira_scope=row.get("jira_scope"),
            notes=row.get("notes"),
        )
        if existing is None:
            session.add(Project(**fields))
            created += 1
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
            updated += 1
    return created, updated


def main() -> None:
    with session_scope() as session:
        pc, pu = seed_people(session)
        jc, ju = seed_projects(session)
        created = ensure_pipelines(session)
    print(f"people:   {pc} created, {pu} updated")
    print(f"projects: {jc} created, {ju} updated")
    print(f"pipelines created: {created or '(none — already present)'}")


if __name__ == "__main__":
    main()
