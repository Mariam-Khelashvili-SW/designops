"""Seed roster + project registry from seeds/*.yaml, and pipeline rows (A1 + A2 + A3).

Idempotent: re-running upserts by natural key (person = first email or full_name;
project = canonical_name; pipeline = key). Local edits made in the UI are preserved
for fields the seed does not own. Run: `python -m scripts.seed`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from designops.core.db import session_scope
from designops.core.models import Person, Pipeline, Project

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


def seed_pipeline(session) -> None:
    """The A1 daily digest pipeline. Ships locked: send_mode=none, go_live=false (§12)."""
    key = "daily-digest"
    existing = session.query(Pipeline).filter_by(key=key).one_or_none()
    if existing is None:
        session.add(
            Pipeline(
                key=key,
                name="A1 — Daily Ops Digest",
                description="Design-team daily ops digest for Olga Kimalana (Head of Design).",
                skill_path="designops/skills/daily-ops-digest.md",
                schedule_cron="0 11 * * 1-5",  # synthesis; ingest is 06:00 (§6)
                timezone="Europe/Riga",
                recipients=[],
                send_mode="none",
                enabled=False,
                go_live=False,
                config={
                    "source_mode": "fairwind",     # §1 switch; gmail dormant
                    "min_coverage": 0.6,
                    "ingest_cron": "0 6 * * 1-5",
                    "carry_forward_blockers": False,  # §9.4 recommendation
                },
            )
        )


def seed_weekly_backlog_pipeline(session) -> None:
    """A3 weekly backlog + availability. Generate-only until deliberately enabled."""
    key = "weekly-backlog"
    existing = session.query(Pipeline).filter_by(key=key).one_or_none()
    if existing is not None:
        return
    session.add(
        Pipeline(
            key=key,
            name="A3 — Weekly Backlog + Availability",
            description=(
                "Monday weekly planned backlog with availability markers for Olga — "
                "Friday dailies + open assigned Jira (remaining hours)."
            ),
            skill_path="designops/skills/weekly-backlog.md",
            schedule_cron="0 11 * * 1",  # Monday 11:00 Europe/Riga
            timezone="Europe/Riga",
            recipients=[],
            send_mode="none",
            enabled=False,
            go_live=False,
            config={
                "source_mode": "fairwind+jira",
                "normal_week_hours": 40,
            },
        )
    )


def seed_weekly_health_pipeline(session) -> None:
    """A2 weekly project health & budget."""
    key = "weekly-health"
    existing = session.query(Pipeline).filter_by(key=key).one_or_none()
    if existing is not None:
        existing.go_live = True
        existing.send_mode = "self"
        return
    session.add(
        Pipeline(
            key=key,
            name="A2 — Weekly Project Health & Budget",
            description=(
                "Monday design-only project health & budget burn for Olga — "
                "full-history Jira by project key + Fairwind client comms."
            ),
            skill_path="designops/skills/weekly-health.md",
            schedule_cron="0 11 * * 1",
            timezone="Europe/Riga",
            recipients=[],
            send_mode="self",
            enabled=False,
            go_live=True,
            config={
                "source_mode": "jira+fairwind",
            },
        )
    )


def main() -> None:
    with session_scope() as session:
        pc, pu = seed_people(session)
        jc, ju = seed_projects(session)
        seed_pipeline(session)
        seed_weekly_backlog_pipeline(session)
        seed_weekly_health_pipeline(session)
    print(f"people:   {pc} created, {pu} updated")
    print(f"projects: {jc} created, {ju} updated")
    print("pipeline: daily-digest ensured (send_mode=none, go_live=false)")
    print("pipeline: weekly-backlog ensured (Mon 11:00, send_mode=none, go_live=false)")
    print("pipeline: weekly-health ensured (go_live=true, send_mode=self)")


if __name__ == "__main__":
    main()
