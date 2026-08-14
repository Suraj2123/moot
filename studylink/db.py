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

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,
    canvas_id   TEXT UNIQUE,
    name        TEXT NOT NULL,
    course_code TEXT,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    id               INTEGER PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    -- 'note' for student notes, 'transcript' for lecture transcripts.
    source_type TEXT NOT NULL DEFAULT 'note',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    -- Chunking parameters that produced this chunk, so a config change can
    -- invalidate exactly the chunks it affects.
    chunk_size    INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_id);

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
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    note_id       INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    relevant      INTEGER NOT NULL,    -- 1 = should match, 0 = should not
    rationale     TEXT DEFAULT '',
    UNIQUE (assignment_id, note_id)
);

CREATE TABLE IF NOT EXISTS work_sessions (
    id            INTEGER PRIMARY KEY,
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
        ):
            conn.execute(f"DELETE FROM {table}")
