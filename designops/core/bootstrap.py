"""Idempotent boot-time defaults. Insert-only — never overwrite UI/schedule edits."""

from __future__ import annotations

from sqlalchemy.orm import Session

from designops.core.models import Pipeline


def ensure_pipelines(session: Session) -> list[str]:
    """Create missing pipeline rows (daily-digest, weekly-backlog, weekly-health).

    Returns keys that were created. Existing rows are left untouched so recipients,
    cron, enabled, and send_mode set in the UI survive restarts.
    """
    created: list[str] = []

    if session.query(Pipeline).filter_by(key="daily-digest").one_or_none() is None:
        session.add(
            Pipeline(
                key="daily-digest",
                name="A1 — Daily Ops Digest",
                description="Design-team daily ops digest for Olga Kimalana (Head of Design).",
                skill_path="designops/skills/daily-ops-digest.md",
                schedule_cron="0 11 * * 1-5",
                timezone="Europe/Riga",
                recipients=[],
                send_mode="none",
                enabled=False,
                go_live=False,
                config={
                    "source_mode": "fairwind",
                    "min_coverage": 0.6,
                    "ingest_cron": "0 6 * * 1-5",
                    "carry_forward_blockers": False,
                },
            )
        )
        created.append("daily-digest")

    if session.query(Pipeline).filter_by(key="weekly-backlog").one_or_none() is None:
        session.add(
            Pipeline(
                key="weekly-backlog",
                name="A3 — Weekly Backlog + Availability",
                description=(
                    "Monday weekly planned backlog with availability markers for Olga — "
                    "Friday dailies + open assigned Jira (remaining hours)."
                ),
                skill_path="designops/skills/weekly-backlog.md",
                schedule_cron="0 11 * * 1",
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
        created.append("weekly-backlog")

    if session.query(Pipeline).filter_by(key="weekly-health").one_or_none() is None:
        session.add(
            Pipeline(
                key="weekly-health",
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
        created.append("weekly-health")

    return created
