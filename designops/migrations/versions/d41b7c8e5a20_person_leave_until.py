"""person.leave_until

Revision ID: d41b7c8e5a20
Revises: c93a1f2e4d70
Create Date: 2026-07-22 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd41b7c8e5a20'
down_revision: Union[str, None] = 'c93a1f2e4d70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("person", sa.Column("leave_until", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("person", "leave_until")
