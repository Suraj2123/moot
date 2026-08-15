"""drop the HNSW index; exact search wins at this scale

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15

0003 added an HNSW index. Benchmarking it (scripts/bench_vectors.py --ef-sweep,
20k vectors across 20 users, top_k=5) showed it is strictly worse than not
having one:

              ef_search   ms/query   recall@5
                    100       3.20      0.560
                    400       7.05      0.933
                   1000      13.65      1.000
         index bypassed       8.74      1.000

Every setting that approaches full recall is slower than simply not using the
index, and the shipped setting loses 44% of true neighbours.

The cause is post-filtering. `embeddings` is multi-tenant, and HNSW cannot apply
the tenant predicate during its graph walk -- it collects `ef_search` candidates
across everyone's vectors and the predicate removes them afterwards, leaving one
tenant roughly ef_search/N. pgvector caps ef_search at 1000, so past a certain
number of tenants full recall is unreachable at any setting.

Dropping the index is therefore not a retreat to something slower. Without it
Postgres does an exact top-N over one user's rows, which at a semester of notes
per user is a few thousand vectors and single-digit milliseconds.

Worth revisiting when any of these change:
  - pgvector >= 0.8, whose iterative index scans keep searching until enough
    rows survive the filter, which is exactly the missing piece here
  - a single user's corpus growing past the point where an exact scan is fast
    enough, which bench_vectors.py will show before users notice
  - partitioning `embeddings` by tenant, giving each tenant its own index

Set STUDYLINK_HNSW=1 to build the index anyway and measure for yourself.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_embeddings_embedding_hnsw"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


def downgrade() -> None:
    """Recreate it, for anyone deliberately measuring the approximate path."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'embeddings' AND column_name = 'embedding'"
        )
    ).first()
    if not exists:
        return
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
