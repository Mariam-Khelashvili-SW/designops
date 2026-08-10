"""project.figma_urls — multiple Figma file URLs per tracked project

Revision ID: k2g8b9c0d1e6
Revises: j1f7a8b9c0d5
Create Date: 2026-08-10 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k2g8b9c0d1e6"
down_revision: Union[str, None] = "j1f7a8b9c0d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "figma_urls",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("project", "figma_urls")
