"""somewhere to put notes

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-25

A flat list of notes works until about thirty of them and then stops working;
one semester produces more than that. Folders are the smallest thing that
fixes it.

One level, deliberately. Nested folders need a tree control, a guard against
moving a folder into its own descendant, and a breadcrumb -- none of which
earns its place before somebody has actually outgrown a single level.

`notes.folder_id` is SET NULL rather than CASCADE. Deleting a folder is a
filing decision, and it must never become a way to lose a semester of notes by
accident. An unfiled note is a normal state, not an error, which is also why
the column is nullable rather than defaulting to some "Inbox" row.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_folders_user_id", "folders", ["user_id"])

    # batch mode: SQLite cannot ALTER a table to add a foreign key.
    with op.batch_alter_table("notes") as batch:
        batch.add_column(sa.Column("folder_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_notes_folder_id_folders", "folders", ["folder_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_notes_folder_id", "notes", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_notes_folder_id", table_name="notes")
    with op.batch_alter_table("notes") as batch:
        batch.drop_constraint("fk_notes_folder_id_folders", type_="foreignkey")
        batch.drop_column("folder_id")
    op.drop_index("ix_folders_user_id", table_name="folders")
    op.drop_table("folders")
