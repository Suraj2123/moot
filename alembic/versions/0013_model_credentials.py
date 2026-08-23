"""let a user bring their own model API key

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23

A new table rather than columns on `users`, for the same two reasons
`canvas_credentials` is one: the lifetimes differ -- removing a key deletes a
row and leaves the account alone -- and a table nobody joins to by default is
one fewer way for a secret to end up in a `SELECT *` somewhere it should not be.

`hint` holds the last four characters so the settings page can show which key
is stored without being able to show the key.

No batch mode here: creating a table is the one schema change SQLite performs
natively, since there is no existing table to ALTER.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("key_version", sa.String(16), nullable=False),
        sa.Column("hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_model_credentials_user_provider"),
    )


def downgrade() -> None:
    op.drop_table("model_credentials")
