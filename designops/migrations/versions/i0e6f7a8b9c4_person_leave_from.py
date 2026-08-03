"""person.leave_from for mid-week leave / PARTIAL availability

Revision ID: i0e6f7a8b9c4
Revises: h9d5e6f7a8b3
Create Date: 2026-08-04 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i0e6f7a8b9c4"
down_revision: Union[str, None] = "h9d5e6f7a8b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("person", sa.Column("leave_from", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("person", "leave_from")
