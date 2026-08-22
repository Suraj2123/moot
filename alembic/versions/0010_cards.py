"""add decks, cards, and review history

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-19

Notes are already chunked, embedded, and matched. What was missing is the part
a student actually does the night before: recall practice. Cards are that, and
they fit the existing grain -- generated from the student's own notes, each one
carrying the sentence it came from, so a card that cannot be traced back to
their material is a bug rather than a feature.

Three tables rather than one:

  * `decks` groups cards, because studying happens per lecture or per exam,
    not over everything at once.
  * `cards` holds the pair plus its scheduling state.
  * `card_reviews` holds every grade ever given.

The third is the one that looks redundant. Keeping only the state on the card
means the scheduling algorithm can never be changed without throwing away the
history it would need to re-derive intervals -- and the first scheduler a
product ships is never the one it keeps.

`note_id` and `course_id` are ON DELETE SET NULL, not CASCADE. Deleting a note
should not destroy a fortnight of review history for cards made from it; the
card survives, it just stops being traceable.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_decks"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_decks_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"],
            name="fk_decks_course_id_courses", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_decks_user_id", "decks", ["user_id"])

    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("front", sa.Text(), nullable=False),
        sa.Column("back", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ease", sa.Integer(), nullable=False, server_default="250"),
        sa.Column("reviews", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lapses", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name="pk_cards"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_cards_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["deck_id"], ["decks.id"], name="fk_cards_deck_id_decks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name="fk_cards_note_id_notes", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_cards_user_id", "cards", ["user_id"])
    op.create_index("ix_cards_deck_id", "cards", ["deck_id"])

    op.create_table(
        "card_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_card_reviews"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_card_reviews_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"], ["cards.id"],
            name="fk_card_reviews_card_id_cards", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_card_reviews_user_id", "card_reviews", ["user_id"])
    op.create_index("ix_card_reviews_card_id", "card_reviews", ["card_id"])


def downgrade() -> None:
    op.drop_table("card_reviews")
    op.drop_table("cards")
    op.drop_table("decks")
