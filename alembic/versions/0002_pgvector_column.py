"""add pgvector extension and native vector column

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15

Postgres only. On SQLite this is a no-op: the portable `vector` blob column
added in 0001 remains the only storage there, and retrieval falls back to the
numpy path.

The dimension comes from EMBEDDING_DIM at migration time because pgvector needs
a declared dimension to build an index on. Changing embedding provider means
writing a follow-up migration that alters this column, not editing a setting:
the hash baseline is 256 wide, voyage-3 is 1024.
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DIM = int(os.environ.get("EMBEDDING_DIM", "256"))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    available = bind.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).first()
    if not available:
        # Managed Postgres without pgvector: leave the blob path in place rather
        # than failing the deploy. Retrieval still works, just in Python.
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS embedding vector({DIM})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS embedding")
