"""add an HNSW index on the native vector column

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15

Postgres only, and a no-op where pgvector is not installed.

This is the commit where retrieval stops being exact. HNSW is an approximate
index: it walks a navigable small-world graph instead of comparing against every
row, which is what makes it sublinear, and the price is that it can miss a true
nearest neighbour. `hnsw.ef_search` buys recall back at the cost of latency --
see studylink/pgvector_support.py, where the app sets it per connection.

Build parameters are pgvector's defaults (m=16, ef_construction=64). They are
spelled out rather than left implicit because raising them later means an index
rebuild, and a reader deciding whether to do that needs to know where it started.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_embeddings_embedding_hnsw"


def _has_vector_column(bind) -> bool:
    return bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'embeddings' AND column_name = 'embedding'"
        )
    ).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_vector_column(bind):
        # 0002 skipped the column because pgvector was unavailable. Nothing to
        # index, and the app is still on the numpy path.
        return

    # `vector_cosine_ops` has to match the operator the query uses (`<=>`).
    # Indexing with the L2 opclass and querying with cosine silently does not use
    # the index at all -- the query still returns correct rows, just slowly, which
    # is the kind of regression that hides for months.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
