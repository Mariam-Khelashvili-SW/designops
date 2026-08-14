"""daily_next_snapshot for repeat detection

Revision ID: n4j0e1f2g3h8
Revises: l3h9c0d1e2f7
Create Date: 2026-08-14 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "n4j0e1f2g3h8"
down_revision: Union[str, None] = "l3h9c0d1e2f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_next_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("person_name", sa.String(), nullable=False),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("next_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("done_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("intent_key", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("hours_logged", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"]),
        sa.UniqueConstraint(
            "report_date",
            "person_name",
            "project",
            name="uq_daily_next_person_project_date",
        ),
    )
    op.create_index(
        "ix_daily_next_snapshot_report_date", "daily_next_snapshot", ["report_date"]
    )
    op.create_index(
        "ix_daily_next_snapshot_person_project",
        "daily_next_snapshot",
        ["person_name", "project"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_daily_next_snapshot_person_project", table_name="daily_next_snapshot"
    )
    op.drop_index("ix_daily_next_snapshot_report_date", table_name="daily_next_snapshot")
    op.drop_table("daily_next_snapshot")
