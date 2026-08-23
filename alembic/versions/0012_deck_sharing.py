"""give decks a visibility, a public slug, and a fork trail

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23

Three columns, one idea: a deck can leave the account that made it.

`visibility` defaults to private and is never inferred from anything. A deck is
built out of a student's own notes, so the only safe default is the one that
shares nothing -- and making the default explicit in the column means a deck
created by code that predates sharing is private too.

`slug` is the public identifier. Not the primary key: a sequential id in a URL
says how many decks exist and invites walking the range. Every deck gets one at
creation, including the ones that already existed when this ran, so a share
link never waits on a write.

`forked_from_id` is SET NULL rather than CASCADE. Someone deleting the deck you
copied should not delete your copy of it.
"""
from __future__ import annotations

import secrets

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Duplicated from studylink/slugs.py rather than imported. A migration has to
# keep producing the same result years from now, and importing application code
# ties this file's behaviour to whatever that module says at the time it runs.
ALPHABET = "23456789bcdfghjkmnpqrstvwxyzBCDFGHJKMNPQRSTVWXYZ"


def _slug() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(10))


def upgrade() -> None:
    # batch_alter_table throughout: SQLite cannot ALTER a table to add a
    # constraint, so the unique index and the self-referential foreign key
    # below both need the copy-and-swap that batch mode performs. On Postgres
    # these compile to ordinary ALTERs.
    with op.batch_alter_table("decks") as batch:
        batch.add_column(
            sa.Column(
                "visibility", sa.String(16), nullable=False, server_default="private"
            )
        )
        batch.add_column(sa.Column("slug", sa.String(16), nullable=True))
        batch.add_column(sa.Column("forked_from_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_decks_forked_from_id_decks", "decks", ["forked_from_id"], ["id"],
            ondelete="SET NULL",
        )

    # Backfill before the unique index exists, so a duplicate is a retry rather
    # than a failed migration. Row by row because each slug is different --
    # there is no set-based way to say "a different random value per row" that
    # both backends agree on.
    connection = op.get_bind()
    existing = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT slug FROM decks WHERE slug IS NOT NULL")
        )
    }
    ids = [row[0] for row in connection.execute(sa.text("SELECT id FROM decks"))]
    for deck_id in ids:
        slug = _slug()
        while slug in existing:
            slug = _slug()
        existing.add(slug)
        connection.execute(
            sa.text("UPDATE decks SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": deck_id},
        )

    op.create_index("ix_decks_visibility", "decks", ["visibility"])
    # Unique, so two decks can never answer for the same URL. Created after the
    # backfill: doing it first would refuse every NULL on Postgres... it would
    # not, actually -- NULLs are distinct there -- but it would leave a window
    # where the backfill could insert a duplicate and only find out at the end.
    op.create_index("uq_decks_slug", "decks", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_decks_slug", table_name="decks")
    op.drop_index("ix_decks_visibility", table_name="decks")
    with op.batch_alter_table("decks") as batch:
        batch.drop_constraint("fk_decks_forked_from_id_decks", type_="foreignkey")
        batch.drop_column("forked_from_id")
        batch.drop_column("slug")
        batch.drop_column("visibility")
