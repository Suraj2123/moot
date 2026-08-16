"""add the sessions table

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-16

Stores the SHA-256 of each session token, never the token. A leaked backup
therefore does not contain working logins.

A plain hash rather than a KDF is deliberate: the token is 256 random bits, so
there is nothing to brute-force, and every authenticated request verifies one --
scrypt here would add ~400ms to every API call and buy nothing.

`expires_at` is absolute rather than sliding, and `revoked_at` is a column
rather than a delete, so a revoked token and a token that never existed stay
distinguishable when you are trying to reconstruct what happened to an account.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    # Every authenticated request looks up by user; the unique constraint above
    # already covers lookup by token_hash.
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
