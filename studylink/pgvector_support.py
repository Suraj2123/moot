"""Postgres-only vector column support.

The portable `embeddings.vector` blob stays the source of truth on both engines.
On Postgres we additionally maintain a native `embedding vector(N)` column,
because that is what pgvector can index and search inside the database instead
of shipping every row to Python.

**The dimension is fixed at migration time**, and that is a genuine operational
constraint rather than a limitation of this code: pgvector can only build an
HNSW index on a column with a declared dimension. Switching embedding provider
(hash-256 -> voyage-3's 1024) therefore needs a migration, not just a config
change. `EMBEDDING_DIM` is the single place that decision is recorded.
"""

from __future__ import annotations

import os

from sqlalchemy import Connection, text

from .schema import add_native_vector_column

DEFAULT_DIM = 256
HNSW_INDEX_NAME = "ix_embeddings_embedding_hnsw"

# pgvector's own default is 40. Raised because this table is multi-tenant: an
# HNSW scan collects candidates across every user's vectors and the tenant
# predicate then discards most of them, so a user holding a small share of the
# corpus can end up with fewer than top_k survivors -- a silent recall loss that
# reads as "the search did not find much" rather than as an error.
#
# Only relevant when STUDYLINK_HNSW built an index; see `hnsw_enabled` for the
# measurements behind that default being off. Reaching full recall on 20 tenants
# needed 800 here, which was already slower than not using the index at all.
DEFAULT_EF_SEARCH = 100


def configured_dim() -> int:
    """The dimension the native vector column is declared with."""
    return int(os.environ.get("EMBEDDING_DIM", DEFAULT_DIM))


def configured_ef_search() -> int:
    """How many candidates HNSW keeps while searching. Higher = better recall, slower."""
    return int(os.environ.get("HNSW_EF_SEARCH", DEFAULT_EF_SEARCH))


def has_hnsw_index(conn: Connection) -> bool:
    if not is_postgres(conn):
        return False
    row = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": HNSW_INDEX_NAME},
    ).first()
    return row is not None


def hnsw_enabled() -> bool:
    """Whether to build the HNSW index. Off by default, and deliberately so.

    Measured on 20k vectors across 20 users, top_k=5 (bench_vectors.py --ef-sweep):

              ef_search   ms/query   recall@5
                    100       3.20      0.560
                    400       7.05      0.933
                   1000      13.65      1.000
         index bypassed       8.74      1.000

    The index is strictly dominated here -- every setting that approaches full
    recall is slower than not using it, and the default setting loses 44% of true
    neighbours. `embeddings` is multi-tenant and HNSW cannot apply the tenant
    predicate during its graph walk, so it gathers candidates across everyone's
    vectors and the predicate discards them afterwards, leaving one tenant
    roughly ef_search/N. ef_search is capped at 1000, so past some number of
    tenants full recall is not reachable at all.

    Without the index Postgres does an exact top-N over one user's rows, which is
    both correct and, at a semester of notes per user, fast.

    Set STUDYLINK_HNSW=1 to build it anyway and measure for yourself. Revisit the
    default on pgvector >= 0.8, whose iterative index scans keep searching until
    enough rows survive the filter -- the missing piece here.
    """
    return os.environ.get("STUDYLINK_HNSW", "").strip().lower() in {"1", "true", "yes"}


def ensure_hnsw_index(conn: Connection) -> bool:
    """Create the HNSW index if it is missing and STUDYLINK_HNSW asks for it."""
    if not hnsw_enabled():
        return False
    if not has_vector_column(conn):
        return False
    if not has_hnsw_index(conn):
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {HNSW_INDEX_NAME} ON embeddings "
                "USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )
        conn.commit()
    return True


def apply_search_tuning(conn: Connection, ef_search: int | None = None) -> None:
    """Set `hnsw.ef_search` for this connection.

    It is a per-session GUC, not a property of the index, so it has to be set on
    every connection rather than once at migration time. Harmless when no index
    exists, which is the default -- it only takes effect if STUDYLINK_HNSW built
    one, and then it is the dial that trades latency for recall.
    """
    if not is_postgres(conn):
        return
    value = ef_search or configured_ef_search()
    conn.execute(text(f"SET hnsw.ef_search = {int(value)}"))


def is_postgres(conn: Connection) -> bool:
    return conn.dialect.name == "postgresql"


def extension_available(conn: Connection) -> bool:
    """Whether the pgvector extension can be installed on this server."""
    if not is_postgres(conn):
        return False
    row = conn.execute(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).first()
    return row is not None


def has_vector_column(conn: Connection) -> bool:
    if not is_postgres(conn):
        return False
    row = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'embeddings' AND column_name = 'embedding'"
        )
    ).first()
    return row is not None


def ensure_vector_column(conn: Connection, dim: int | None = None) -> bool:
    """Install pgvector and add the native column if they are missing.

    Alembic owns this for real deployments; this exists so tests and local
    `create_all` setups get the same shape without running migrations. Returns
    True if the column is present afterwards.
    """
    if not is_postgres(conn):
        return False
    if not extension_available(conn):
        return False

    dim = dim or configured_dim()
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    add_native_vector_column(dim)
    if not has_vector_column(conn):
        conn.execute(
            text(f"ALTER TABLE embeddings ADD COLUMN embedding vector({dim})")
        )
    conn.commit()
    return True


def backfill_native_vectors(conn: Connection, batch_size: int = 500) -> int:
    """Copy the portable blob into the native column for rows that predate it.

    Migration 0002 adds the column but cannot fill it: the blob is a packed
    float32 buffer, and unpacking it is Python's job, not SQL's. Without this, a
    database that had embeddings before the migration ends up with a column that
    is half NULL -- and a search that reads the native column would silently
    return fewer results rather than fail, which is the worst way for this to go
    wrong.

    So the search path is allowed to trust the column completely, and this is what
    earns that trust. Runs on connect; after the first pass the WHERE matches
    nothing and it costs one indexed lookup. Returns the number of rows filled.
    """
    if not has_vector_column(conn):
        return 0

    import numpy as np

    filled = 0
    while True:
        rows = conn.execute(
            text(
                "SELECT owner_type, owner_id, model, vector FROM embeddings "
                "WHERE embedding IS NULL LIMIT :n"
            ),
            {"n": batch_size},
        ).all()
        if not rows:
            break

        conn.execute(
            text(
                "UPDATE embeddings SET embedding = :embedding "
                "WHERE owner_type = :owner_type AND owner_id = :owner_id "
                "AND model = :model"
            ),
            [
                {
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "model": model,
                    "embedding": to_pgvector_literal(
                        np.frombuffer(blob, dtype=np.float32)
                    ),
                }
                for owner_type, owner_id, model, blob in rows
            ],
        )
        conn.commit()
        filled += len(rows)

    return filled


def to_pgvector_literal(values) -> str:
    """Render a vector in pgvector's text input format: `[1,2,3]`.

    Needed for `text()` queries, where there is no `Vector` column type in play
    to do the encoding -- psycopg2 has no adapter for a bare ndarray.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def to_vector_param(values) -> "np.ndarray":
    """Coerce a vector into something the `Vector` column type will accept.

    pgvector's SQLAlchemy type does its own encoding and only takes a list or an
    ndarray -- hand it a pre-rendered `'[1,2,3]'` string and it raises. Vectors
    reach here as whatever the provider returned (often a memoryview off the
    blob column), so normalise to float32, which is also pgvector's storage type.
    """
    import numpy as np

    return np.asarray(values, dtype=np.float32)
