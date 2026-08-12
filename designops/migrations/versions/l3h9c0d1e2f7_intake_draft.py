"""intake_draft table for design intake generator

Revision ID: l3h9c0d1e2f7
Revises: k2g8b9c0d1e6
Create Date: 2026-08-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l3h9c0d1e2f7"
down_revision: Union[str, None] = "k2g8b9c0d1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "intake_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("pasted_input", sa.Text(), nullable=False),
        sa.Column("estimate_link", sa.String(), nullable=True),
        sa.Column("proposal_link", sa.String(), nullable=True),
        sa.Column("estimate_rows", sa.Text(), nullable=True),
        sa.Column("corrections", sa.Text(), nullable=True),
        sa.Column(
            "uploaded_files",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "sections_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("intake_report", sa.Text(), nullable=True),
        sa.Column(
            "flags",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notion_page_url", sa.String(), nullable=True),
        sa.Column("notion_page_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("runner", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_intake_draft_generated_at", "intake_draft", ["generated_at"])
    op.create_index("ix_intake_draft_status", "intake_draft", ["status"])


def downgrade() -> None:
    op.drop_index("ix_intake_draft_status", table_name="intake_draft")
    op.drop_index("ix_intake_draft_generated_at", table_name="intake_draft")
    op.drop_table("intake_draft")
