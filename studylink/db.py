"""SQLite storage layer.

SQLite is enough for a single-user MVP: the whole corpus is a few thousand
chunks, and keeping vectors in the same file as the metadata means there is
exactly one thing to back up, inspect, or delete.

Embeddings are stored as raw float32 bytes keyed by `(owner_type, owner_id,
model)`. Keying on the model name means switching embedding providers does not
silently mix vector spaces -- the old vectors stay put and are simply not read.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool

from .migrations import apply_all
from .schema import metadata

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- One row per person using StudyLink. `apple_sub` is the stable subject claim
-- from Sign in with Apple; it is null for locally-created users so the CLI and
-- the demo seeder work without an auth round trip.
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    apple_sub  TEXT UNIQUE,
    email      TEXT,
    display_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    canvas_id   TEXT,
    name        TEXT NOT NULL,
    course_code TEXT,
    synced_at   TEXT,
    -- Uniqueness is per user: two students can both be enrolled in Canvas
    -- course 101, and those are different rows.
    UNIQUE (user_id, canvas_id)
);
CREATE INDEX IF NOT EXISTS idx_courses_user ON courses(user_id);

CREATE TABLE IF NOT EXISTS assignments (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER REFERENCES users(id) ON DELETE CASCADE,
    canvas_id        TEXT,
    course_id        INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT DEFAULT '',
    due_at           TEXT,
    points_possible  REAL,
    submission_types TEXT DEFAULT '',
    html_url         TEXT,
    synced_at        TEXT,
    UNIQUE (course_id, canvas_id)
);
CREATE INDEX IF NOT EXISTS idx_assignments_user ON assignments(user_id);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    course_id   INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    -- 'note' for student notes, 'transcript' for lecture transcripts.
    source_type TEXT NOT NULL DEFAULT 'note',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    -- Denormalised from notes. Retrieval filters millions of chunk vectors by
    -- owner on every query; a join to notes for that is a per-query cost paid
    -- to avoid a column that never changes independently.
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    -- Chunking parameters that produced this chunk, so a config change can
    -- invalidate exactly the chunks it affects.
    chunk_size    INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id);

CREATE TABLE IF NOT EXISTS embeddings (
    owner_type TEXT NOT NULL,          -- 'chunk' | 'assignment'
    owner_id   INTEGER NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    PRIMARY KEY (owner_type, owner_id, model)
);

CREATE TABLE IF NOT EXISTS eval_labels (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    note_id       INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    relevant      INTEGER NOT NULL,    -- 1 = should match, 0 = should not
    rationale     TEXT DEFAULT '',
    UNIQUE (assignment_id, note_id)
);

CREATE TABLE IF NOT EXISTS work_sessions (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    mode          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_messages (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY,
    ran_at      TEXT NOT NULL,
    courses     INTEGER NOT NULL,
    assignments INTEGER NOT NULL,
    detail      TEXT DEFAULT ''
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and initialise, if needed) the database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # SCHEMA creates a fresh database; migrations bring an existing one forward.
    apply_all(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """Drop all rows but keep the schema. Used by the demo seeder and tests."""
    with transaction(conn):
        for table in (
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
        ):
            conn.execute(f"DELETE FROM {table}")


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
    """
    metadata.create_all(engine)
