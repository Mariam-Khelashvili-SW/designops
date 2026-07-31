"""person jira_verified + fairwind_verified

Revision ID: h9d5e6f7a8b3
Revises: g8c4d5e6f7a2
Create Date: 2026-07-31 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h9d5e6f7a8b3"
down_revision: Union[str, None] = "g8c4d5e6f7a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "person",
        sa.Column(
            "jira_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "person",
        sa.Column(
            "fairwind_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Backfill from the old combined flag.
    op.execute(
        """
        UPDATE person
        SET jira_verified = true, fairwind_verified = true
        WHERE identity_verified = true
        """
    )


def downgrade() -> None:
    op.drop_column("person", "fairwind_verified")
    op.drop_column("person", "jira_verified")
