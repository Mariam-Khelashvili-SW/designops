"""project weekly health budget fields

Revision ID: f7b3c4d5e6a1
Revises: e6a2c1904f83
Create Date: 2026-07-27 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7b3c4d5e6a1"
down_revision: Union[str, None] = "e6a2c1904f83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "track_weekly_health",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("project", sa.Column("display_subtitle", sa.String(), nullable=True))
    op.add_column("project", sa.Column("signed_design_estimate_h", sa.Float(), nullable=True))
    op.add_column("project", sa.Column("estimate_basis", sa.String(), nullable=True))
    op.add_column(
        "project",
        sa.Column(
            "agreement_summary",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column("project", sa.Column("jira_scope", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "jira_scope")
    op.drop_column("project", "agreement_summary")
    op.drop_column("project", "estimate_basis")
    op.drop_column("project", "signed_design_estimate_h")
    op.drop_column("project", "display_subtitle")
    op.drop_column("project", "track_weekly_health")
