"""The native pgvector column, and the promise that SQLite never sees it.

Two claims are worth testing here, and they pull in opposite directions:

1. On SQLite the whole pgvector layer is inert. Nothing queries for the column,
   nothing tries to write it, and the portable blob keeps working exactly as it
   did before this commit. This runs everywhere.
2. On Postgres the native column is actually populated, and it agrees with the
   blob. A column full of NULLs would pass any "does the column exist" check and
   still make the index useless, so the real assertion is numeric agreement.

The second set needs a live server, so it is opt-in via
STUDYLINK_TEST_POSTGRES_URL and skipped otherwise.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from sqlalchemy import select, text

from studylink import store
from studylink.db import connect
from studylink.indexing import Indexer
from studylink.pgvector_support import (
    configured_dim,
    ensure_vector_column,
    extension_available,
    has_vector_column,
    is_postgres,
    to_vector_param,
)
from studylink.schema import embeddings

POSTGRES_URL = os.environ.get("STUDYLINK_TEST_POSTGRES_URL", "")
requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="set STUDYLINK_TEST_POSTGRES_URL to run the Postgres tests"
)


# ------------------------------------------------------------------ portable


def test_vector_param_is_float32():
    """pgvector stores float32; sending float64 would silently round-trip lossy."""
    param = to_vector_param([1.0, 2.0, 3.0])
    assert param.dtype == np.float32
    assert param.tolist() == [1.0, 2.0, 3.0]


def test_vector_param_accepts_a_memoryview():
    """Vectors arrive off the blob column as buffers, not lists."""
    original = np.array([0.5, 0.25, 0.125], dtype=np.float32)
    param = to_vector_param(memoryview(original.tobytes()).cast("f"))
    assert np.array_equal(param, original)


def test_sqlite_is_not_postgres(conn):
    assert is_postgres(conn) is False
    assert extension_available(conn) is False
    assert has_vector_column(conn) is False


def test_ensure_vector_column_is_a_no_op_on_sqlite(conn):
    """The migration and create_all both call this; on SQLite it must do nothing."""
    assert ensure_vector_column(conn) is False


def test_sqlite_still_stores_and_reads_embeddings(conn, provider, config, user_id):
    """The portable blob path is unchanged -- this is the regression guard."""
    store.create_note(conn, "Note", "Gradient descent steps downhill.", user_id=user_id)
    Indexer(conn, provider, config, user_id).reindex()

    rows = conn.execute(
        select(embeddings.c.dim, embeddings.c.vector).where(
            embeddings.c.owner_type == "chunk"
        )
    ).all()

    assert rows, "indexing wrote no chunk embeddings"
    for dim, blob in rows:
        assert len(np.frombuffer(blob, dtype=np.float32)) == dim


# ------------------------------------------------------------------ postgres


@pytest.fixture
def pg_conn():
    connection = connect(POSTGRES_URL)
    from studylink.db import reset

    reset(connection)
    yield connection
    connection.close()


@requires_postgres
def test_postgres_gets_the_native_column(pg_conn):
    assert is_postgres(pg_conn) is True
    assert extension_available(pg_conn) is True
    # `connect` runs create_all, which installs the extension and the column.
    assert has_vector_column(pg_conn) is True


@requires_postgres
def test_native_column_is_populated_not_just_present(pg_conn, provider, config):
    """A column of NULLs would satisfy every schema check and index nothing."""
    user_id = store.get_or_create_default_user(pg_conn)
    store.create_note(
        pg_conn,
        "Gradient descent",
        "Gradient descent minimises a loss by stepping downhill. The learning "
        "rate alpha controls the step size.",
        user_id=user_id,
    )
    Indexer(pg_conn, provider, config, user_id).reindex()

    total, populated = pg_conn.execute(
        text("SELECT count(*), count(embedding) FROM embeddings")
    ).one()

    assert total > 0
    assert populated == total


@requires_postgres
def test_native_column_agrees_with_the_portable_blob(pg_conn, provider, config):
    """Both copies of the same vector must rank the corpus identically.

    This is the claim the next commit depends on: swapping the numpy scan for a
    pgvector query has to be a performance change, not a behaviour change.
    """
    user_id = store.get_or_create_default_user(pg_conn)
    for title, body in [
        ("Gradient descent", "Gradient descent steps downhill; alpha sets step size."),
        ("Alliances", "By 1907 Europe had split into the Triple Alliance and Entente."),
        ("Marshall Plan", "Aid flowed to western Europe after 1945."),
    ]:
        store.create_note(pg_conn, title, body, user_id=user_id)
    Indexer(pg_conn, provider, config, user_id).reindex()

    rows = pg_conn.execute(
        text("SELECT owner_id, vector FROM embeddings WHERE owner_type = 'chunk'")
    ).all()
    blobs = {owner_id: np.frombuffer(blob, dtype=np.float32) for owner_id, blob in rows}
    assert len(blobs) >= 3

    query = blobs[min(blobs)]

    def cosine(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    by_numpy = [oid for _, oid in sorted(
        ((cosine(query, v), oid) for oid, v in blobs.items()), reverse=True
    )]
    by_pgvector = [
        row[0]
        for row in pg_conn.execute(
            text(
                "SELECT owner_id FROM embeddings WHERE owner_type = 'chunk' "
                "ORDER BY embedding <=> :q"
            ),
            {"q": "[" + ",".join(repr(float(x)) for x in query) + "]"},
        ).all()
    ]

    assert by_numpy == by_pgvector


@requires_postgres
def test_postgres_metadata_does_not_leak_a_vector_type_into_sqlite(pg_conn, tmp_path):
    """Connecting to Postgres first must not poison SQLite's DDL.

    `metadata` is process-global, so touching Postgres appends the native column
    to it. SQLite's dynamic typing accepts `VECTOR(256)` as a column type without
    complaint, which would let this whole suite pass against a schema no engine
    really supports. The variant in `add_native_vector_column` is what stops that,
    and this asserts it in the order that triggers the bug.
    """
    assert has_vector_column(pg_conn) is True  # metadata now carries the column

    sqlite_conn = connect(tmp_path / "after_postgres.db")
    try:
        # Ask SQLite for the column's declared type rather than grepping the DDL:
        # the portable blob column is itself named `vector`, so a substring check
        # against the whole CREATE TABLE always matches and proves nothing.
        declared = {
            row[1]: row[2]
            for row in sqlite_conn.execute(
                text("PRAGMA table_info(embeddings)")
            ).all()
        }
        assert declared["embedding"] == "BLOB"
        assert has_vector_column(sqlite_conn) is False
    finally:
        sqlite_conn.close()


@requires_postgres
def test_native_column_uses_the_configured_dimension(pg_conn):
    """pgvector can only index a column with a declared dimension."""
    dim = pg_conn.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'embeddings'::regclass AND attname = 'embedding'"
        )
    ).scalar()
    assert dim == configured_dim()
