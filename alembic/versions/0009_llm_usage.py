"""add a per-call LLM usage ledger

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-17

An LLM in the product means spend that scales with use, by a user who is not
paying for it. Without a record there is no way to answer "who is this bill
from" until the invoice arrives.

A ledger rather than a counter on `users`: the questions worth asking are "what
has this account spent this month" and "what did that conversation cost", and a
running total answers neither and cannot be recomputed when it drifts.

Cost is stored in micro-dollars as an integer. Money in a float stops adding up,
and a single call costs far less than a cent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_usage"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_llm_usage_user_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_user_created", "llm_usage", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_id", table_name="llm_usage")
    op.drop_table("llm_usage")
