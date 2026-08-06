"""call_summary_draft table for client email drafts from design calls

Revision ID: j1f7a8b9c0d5
Revises: i0e6f7a8b9c4
Create Date: 2026-08-06 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j1f7a8b9c0d5"
down_revision: Union[str, None] = "i0e6f7a8b9c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "call_summary_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("transcript_id", sa.String(), nullable=False),
        sa.Column("transcript_name", sa.String(), nullable=True),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("reviewer_notes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("extraction_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("transcript_quality", sa.String(), nullable=True),
        sa.Column("low_confidence", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("placeholder_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "designer_recipient_emails",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("owner_name", sa.String(), nullable=True),
        sa.Column("policy_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("policy_block_reason", sa.Text(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_call_summary_draft_transcript_id", "call_summary_draft", ["transcript_id"])
    op.create_index("ix_call_summary_draft_generated_at", "call_summary_draft", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_call_summary_draft_generated_at", table_name="call_summary_draft")
    op.drop_index("ix_call_summary_draft_transcript_id", table_name="call_summary_draft")
    op.drop_table("call_summary_draft")
