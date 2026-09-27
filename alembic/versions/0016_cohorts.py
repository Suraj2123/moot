"""Explicit, revocable cohort note sharing.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cohorts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("term", sa.String(80), nullable=False),
        sa.Column("invite_code", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cohort_memberships",
        sa.Column("cohort_id", sa.Integer(), sa.ForeignKey("cohorts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('member', 'instructor')", name=op.f("ck_cohort_memberships_role")),
        sa.CheckConstraint("status IN ('active', 'left', 'removed')", name=op.f("ck_cohort_memberships_status")),
    )
    op.create_index("ix_cohort_memberships_user_id", "cohort_memberships", ["user_id"])
    with op.batch_alter_table("notes") as batch:
        batch.add_column(sa.Column("visibility", sa.String(16), nullable=False, server_default="private"))
        batch.add_column(sa.Column("shared_cohort_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_notes_shared_cohort_id_cohorts", "cohorts", ["shared_cohort_id"], ["id"])
        batch.create_check_constraint(op.f("ck_notes_sharing"), "(visibility = 'private' AND shared_cohort_id IS NULL) OR (visibility = 'cohort' AND shared_cohort_id IS NOT NULL)")
    op.create_index("ix_notes_shared_cohort_id", "notes", ["shared_cohort_id"])


def downgrade():
    op.drop_index("ix_notes_shared_cohort_id", table_name="notes")
    with op.batch_alter_table("notes") as batch:
        batch.drop_constraint(op.f("ck_notes_sharing"), type_="check")
        batch.drop_constraint("fk_notes_shared_cohort_id_cohorts", type_="foreignkey")
        batch.drop_column("shared_cohort_id")
        batch.drop_column("visibility")
    op.drop_table("cohort_memberships")
    op.drop_table("cohorts")
