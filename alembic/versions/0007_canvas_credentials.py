"""add per-user canvas credentials

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-17

Canvas credentials were read from CANVAS_API_URL and CANVAS_API_TOKEN, which is
one token for the whole deployment. That was correct when there was one user and
is wrong now: every account would sync against whoever's token is in the
environment.

The token is stored encrypted (see studylink/vault.py) and this table never
holds a readable one. It is a separate table rather than columns on `users`
because the lifetimes differ -- disconnecting Canvas removes a row here and
leaves the account alone -- and because a table nobody joins to by default is
one fewer way for a token to turn up in a SELECT * somewhere it should not.

key_version is denormalised out of the ciphertext so the rotation script can
find rows to re-encrypt with an indexed query instead of parsing every value.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("api_url", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("key_version", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_canvas_credentials"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_canvas_credentials_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", name="uq_canvas_credentials_user_id"),
    )
    op.create_index(
        "ix_canvas_credentials_key_version", "canvas_credentials", ["key_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_canvas_credentials_key_version", table_name="canvas_credentials")
    op.drop_table("canvas_credentials")
