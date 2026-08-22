"""link decks to notes and give cards a stable identity

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-22

Cards can now be declared inside a note (`term :: definition`), which means
they are re-derived every time that note is saved. Re-deriving needs a way to
recognise a card the student already has, or every edit would delete the deck
and rebuild it -- discarding the review history, which is the only part of a
flashcard that is expensive to recreate.

`cards.source_key` is that identity: a hash of the question as asked. Editing
an answer keeps the schedule; rewriting a question makes a new card. It is
nullable because generated cards have no such key -- they are authored once and
never re-derived.

`decks.note_id` and `decks.source` let a note own exactly one deck of its own
cards, kept in step with its text, alongside any number of decks generated from
prose. Without the distinction, a sync would have to guess which cards it was
allowed to delete.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table, not plain add_column: SQLite cannot ALTER a table to
    # add a foreign key, so Alembic emulates it by building a new table,
    # copying the rows, and swapping. On Postgres this compiles to the ordinary
    # ALTERs. Writing the Postgres-only version is the mistake that passes CI
    # on one backend and fails on the other at deploy time.
    with op.batch_alter_table("decks") as batch:
        batch.add_column(sa.Column("note_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("source", sa.String(16), nullable=False, server_default="generated")
        )
        batch.create_foreign_key(
            "fk_decks_note_id_notes", "notes", ["note_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_decks_note_id", "decks", ["note_id"])

    with op.batch_alter_table("cards") as batch:
        batch.add_column(sa.Column("source_key", sa.String(32), nullable=True))
    # Looked up per card on every note save, so it wants an index even though
    # the table is small today.
    op.create_index("ix_cards_source_key", "cards", ["deck_id", "source_key"])


def downgrade() -> None:
    op.drop_index("ix_cards_source_key", table_name="cards")
    with op.batch_alter_table("cards") as batch:
        batch.drop_column("source_key")
    op.drop_index("ix_decks_note_id", table_name="decks")
    with op.batch_alter_table("decks") as batch:
        batch.drop_constraint("fk_decks_note_id_notes", type_="foreignkey")
        batch.drop_column("source")
        batch.drop_column("note_id")
