"""let a study target exist without Canvas

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-23

The matching engine -- the retriever, the evidence layer, the calibrated
confidence, the whole evaluation harness behind it -- could only ever be
reached by connecting Canvas. `upsert_assignment` had no caller outside
`canvas.py`, `assignments.course_id` was NOT NULL, and a course only came from
a sync. So the feature this product is built around did nothing at all for
anyone whose university disables student access tokens.

Two columns' worth of change fixes that:

  * `course_id` becomes nullable, because a topic someone types before an exam
    belongs to no course.
  * `source` records where the row came from: "canvas" for a synced assignment,
    "manual" for one a student wrote. The sync must not delete or overwrite
    what a student typed, and without this it cannot tell the difference.

The table keeps its name. Renaming it to `study_targets` would be the tidier
concept, and it would also rewrite `eval_labels`, the evaluation harness, the
retriever's owner_type, and every test that names an assignment -- a large diff
whose only product is vocabulary. The user-facing name is where that vocabulary
actually matters, and it is not stored here.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch mode for both: SQLite implements a nullability change by rebuilding
    # the table, and cannot ALTER a column at all.
    with op.batch_alter_table("assignments") as batch:
        batch.alter_column("course_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(
            sa.Column("source", sa.String(16), nullable=False, server_default="canvas")
        )

    # Existing rows all came from a sync, which the server_default above
    # already says. Stated explicitly so a later reader does not have to infer
    # it from the default.
    op.execute("UPDATE assignments SET source = 'canvas' WHERE source IS NULL")
    op.create_index("ix_assignments_source", "assignments", ["source"])


def downgrade() -> None:
    # Manual targets have no course, so they cannot survive course_id going
    # back to NOT NULL. Dropping them is the only honest answer -- silently
    # attaching them to an arbitrary course would be worse.
    op.execute("DELETE FROM assignments WHERE source = 'manual'")
    op.drop_index("ix_assignments_source", table_name="assignments")
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("source")
        batch.alter_column("course_id", existing_type=sa.Integer(), nullable=False)
