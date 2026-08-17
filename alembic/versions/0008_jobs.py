"""add a jobs table for background work

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-17

Indexing and Canvas sync both run inside the request that triggers them.
Adding one note re-chunks and re-embeds, and a Canvas sync makes a paginated
series of HTTP calls to somebody else's server -- neither is something to hold a
browser connection open for, and a sync that takes longer than the proxy's
timeout fails in a way the user cannot act on.

Rows here are the queue and the audit trail at once, which is the right trade at
this size: a real broker is a second piece of infrastructure to run and back up,
and "SELECT the oldest queued row and mark it running" is correct as long as the
claim is atomic. See studylink/jobs.py for how that claim is made.

`detail` holds a short summary or an error message, never a traceback -- it is
returned to the user, and tracebacks leak paths and sometimes credentials out of
frame locals.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_jobs_user_id_users", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
