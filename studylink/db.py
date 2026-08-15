"""Database connections and transactions.

The schema itself now lives in `schema.py` as SQLAlchemy metadata, and changes
to it are applied with Alembic. This module is just connection management: build
an engine for whichever backend the URL points at, hand out connections, and
give callers one transaction helper.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Connection, Engine, create_engine, delete, event
from sqlalchemy.pool import NullPool

from .schema import metadata

# Deletion order matters: children before parents, or the foreign keys that
# SQLite now enforces will reject the delete.
_RESET_ORDER = (
    "work_messages",
    "work_sessions",
    "eval_labels",
    "embeddings",
    "chunks",
    "notes",
    "assignments",
    "courses",
    "sync_log",
    "users",
)


def connect(db_path_or_url: Path | str) -> Connection:
    """Open a connection, creating any missing tables.

    Accepts either a filesystem path (treated as SQLite, which is what local
    development and the tests pass) or a full SQLAlchemy URL.
    """
    url = str(db_path_or_url)
    if "://" not in url:
        path = Path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"

    engine = make_engine(url)
    create_all(engine)
    connection = engine.connect()
    # Keep the engine reachable so callers can dispose of it, and so the pool is
    # not garbage-collected out from under a long-lived connection.
    connection.info["engine"] = engine
    return connection


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def reset(conn: Connection) -> None:
    """Drop all rows but keep the schema. Used by the demo seeder and tests."""
    with transaction(conn):
        for table_name in _RESET_ORDER:
            conn.execute(delete(metadata.tables[table_name]))


# --------------------------------------------------------------- SQLAlchemy


def make_engine(url: str, echo: bool = False) -> Engine:
    """Build an engine for either backend, with the per-dialect settings each needs.

    SQLite needs foreign keys switched on per connection -- they are off by
    default, which silently turns every `ON DELETE CASCADE` in the schema into a
    no-op and lets orphaned rows accumulate.

    Postgres gets `pool_pre_ping` because hosted databases drop idle connections;
    without it the first request after a quiet period fails with a stale-socket
    error instead of transparently reconnecting.
    """
    if url.startswith("sqlite"):
        engine = create_engine(url, future=True, echo=echo, poolclass=NullPool)

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    return create_engine(url, future=True, echo=echo, pool_pre_ping=True, pool_size=5)


def create_all(engine: Engine) -> None:
    """Create any missing tables from the schema metadata.

    Convenient for tests and first-run local setup. Anything with data in it
    should be migrated with Alembic instead, so schema changes are reviewable.

    On Postgres this also installs pgvector and the native vector column, so a
    create_all database and a migrated one have the same shape -- otherwise
    tests would exercise a schema production never runs.
    """
    metadata.create_all(engine)

    from .pgvector_support import backfill_native_vectors, ensure_vector_column

    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            if ensure_vector_column(conn):
                # Rows written before the column existed have a blob but no
                # native vector. Fill them now so search can trust the column
                # instead of quietly ranking a subset of the corpus.
                backfill_native_vectors(conn)
