"""add password credentials and case-insensitive email uniqueness

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16

Two changes, and the second is the one with teeth.

`password_hash` is nullable. Accounts that never set a password -- the seeded
local user, and later anyone arriving through an identity provider -- simply do
not have one, and that absence is what makes password login impossible for them.
The alternative, a sentinel value meaning "no password", is the kind of thing
that eventually gets compared against and matches.

Email uniqueness is enforced on `lower(email)`, not on `email`. Both Postgres
and SQLite compare strings case-sensitively by default, so a plain unique
constraint happily accepts Alice@school.edu alongside alice@school.edu, and a
login then resolves to whichever row the query happens to find first. Signup
normalises to lowercase, but the index is what makes that a guarantee rather
than a convention -- it also catches rows written by anything that forgets to.

Existing rows are left alone. If a database already holds two addresses that
differ only in case, this migration fails on the index build rather than
silently picking a winner, which is the correct outcome: a human has to decide
which account is real.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

EMAIL_INDEX = "uq_users_email_lower"


def upgrade() -> None:
    bind = op.get_bind()

    # batch_alter_table so SQLite gets its table-rebuild dance; on Postgres this
    # compiles to a plain ALTER.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(255), nullable=True))

    # One statement for both engines: SQLite has supported expression indexes
    # since 3.9, and the syntax is identical.
    op.execute(f"CREATE UNIQUE INDEX {EMAIL_INDEX} ON users (lower(email))")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {EMAIL_INDEX}")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("password_hash")
