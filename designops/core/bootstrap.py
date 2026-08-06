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
                # Same gate as weekly: schedule "Email recipients" + run-page Send
                # can deliver; manual Generate still forces send_mode=none.
                go_live=True,
                config={
                    "source_mode": "fairwind",
                    "min_coverage": 0.6,
                    "ingest_cron": "0 6 * * 1-5",
                    "carry_forward_blockers": False,
                },
            )
        )
        created.append("daily-digest")
    else:
        # Promote delivery gate so schedule "Email recipients" + run-page Send work
        # (Generate remains send_mode=none). Same pattern as weekly pipelines.
        daily = session.query(Pipeline).filter_by(key="daily-digest").one()
        if not daily.go_live:
            daily.go_live = True
            session.add(daily)
            created.append("daily-digest:go_live")

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
                schedule_cron="0 11 * * mon",
                timezone="Europe/Riga",
                recipients=[],
                send_mode="none",
                enabled=False,
                go_live=True,
                config={
                    "source_mode": "fairwind+jira",
                    "normal_week_hours": 40,
                },
            )
        )
        created.append("weekly-backlog")
    else:
        # Promote delivery gate so schedule "Email recipients" can actually send.
        wb = session.query(Pipeline).filter_by(key="weekly-backlog").one()
        changed = False
        if not wb.go_live:
            wb.go_live = True
            changed = True
        # Fix only the known bad default (empty / numeric Tuesday-as-1); never
        # overwrite a deliberate Mon/Tue/… choice the user saved in the UI.
        cron = (wb.schedule_cron or "").strip()
        if cron in ("", "0 11 * * 1"):
            wb.schedule_cron = "0 11 * * mon"
            changed = True
        if changed:
            session.add(wb)
            created.append("weekly-backlog:schedule")

    if session.query(Pipeline).filter_by(key="weekly-health").one_or_none() is None:
        session.add(
            Pipeline(
                key="weekly-health",
                name="A2 — Weekly Project Health & Budget",
                description=(
                    "Tuesday design-only project health & budget burn for Olga — "
                    "full-history Jira by project key + Fairwind client comms."
                ),
                skill_path="designops/skills/weekly-health.md",
                schedule_cron="0 12 * * tue",
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
    else:
        wh = session.query(Pipeline).filter_by(key="weekly-health").one()
        changed = False
        if not wh.go_live:
            wh.go_live = True
            changed = True
        # Weekly project report: Tuesday 12:00 Riga
        cron = (wh.schedule_cron or "").strip()
        if cron in ("", "0 11 * * 1", "0 12 * * 1", "0 11 * * mon") or cron.endswith(
            " * * 1"
        ):
            wh.schedule_cron = "0 12 * * tue"
            changed = True
        if changed:
            session.add(wh)
            created.append("weekly-health:schedule")

    return created
