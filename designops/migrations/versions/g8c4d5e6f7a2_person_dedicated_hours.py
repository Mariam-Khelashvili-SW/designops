"""person dedicated designer weekly hours

Revision ID: g8c4d5e6f7a2
Revises: f7b3c4d5e6a1
Create Date: 2026-07-31 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g8c4d5e6f7a2"
down_revision: Union[str, None] = "f7b3c4d5e6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "person",
        sa.Column(
            "is_dedicated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "person",
        sa.Column("dedicated_weekly_hours", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("person", "dedicated_weekly_hours")
    op.drop_column("person", "is_dedicated")
