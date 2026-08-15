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
    HNSW_INDEX_NAME,
    configured_dim,
    ensure_vector_column,
    extension_available,
    has_vector_column,
    is_postgres,
    to_pgvector_literal,
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


# ------------------------------------------------- native search equivalence


@pytest.fixture
def pg_corpus(pg_conn, provider, config):
    """One user, three notes on clearly different topics, indexed on Postgres."""
    user_id = store.get_or_create_default_user(pg_conn)
    course = store.upsert_course(pg_conn, "1", "CS 101", "CS101", user_id=user_id)
    for title, body in [
        ("Gradient descent", "Gradient descent minimises a loss by stepping downhill. "
                             "The learning rate alpha controls the step size."),
        ("Alliances", "By 1907 Europe had split into two blocs: the Triple Alliance "
                      "faced the Triple Entente."),
        ("Marshall Plan", "The Marshall Plan transferred aid to western Europe after "
                          "1945 and tied recipient economies to the United States."),
    ]:
        store.create_note(pg_conn, title, body, course_id=course, user_id=user_id)
    Indexer(pg_conn, provider, config, user_id).reindex()
    return {"conn": pg_conn, "user_id": user_id, "course": course}


def _query_vector(pg_corpus, provider):
    return provider.embed(["learning rate and gradient descent convergence"])[0]


@requires_postgres
def test_native_search_matches_numpy_exactly(pg_corpus, provider):
    """The whole justification for this commit: same answers, different machine.

    Comparing the two implementations against each other on the same rows is the
    only assertion that actually protects the eval numbers -- if pgvector ranked
    even slightly differently, every metric in the report would shift for a
    reason that has nothing to do with retrieval quality.
    """
    from studylink.vectorstore import VectorStore

    conn, user_id = pg_corpus["conn"], pg_corpus["user_id"]
    store_ = VectorStore(conn)
    query = _query_vector(pg_corpus, provider)

    native = store_.search(query, "chunk", provider.name, user_id, top_k=10)
    numpy_path = store_._search_numpy(query, "chunk", provider.name, user_id, 10, ())

    assert [oid for oid, _ in native] == [oid for oid, _ in numpy_path]
    for (_, a), (_, b) in zip(native, numpy_path):
        assert a == pytest.approx(b, abs=1e-6)


@requires_postgres
def test_native_search_is_actually_the_path_taken(pg_corpus, provider):
    """Guard against the dispatch silently falling back to numpy on Postgres."""
    from studylink.vectorstore import VectorStore

    assert has_vector_column(pg_corpus["conn"]) is True
    calls = []
    store_ = VectorStore(pg_corpus["conn"])
    original = store_._search_numpy
    store_._search_numpy = lambda *a, **k: (calls.append(1), original(*a, **k))[1]

    store_.search(_query_vector(pg_corpus, provider), "chunk", provider.name,
                  pg_corpus["user_id"], top_k=3)

    assert calls == [], "search fell back to the numpy path on Postgres"


@requires_postgres
def test_native_search_honours_top_k_and_exclusions(pg_corpus, provider):
    from studylink.vectorstore import VectorStore

    store_ = VectorStore(pg_corpus["conn"])
    query = _query_vector(pg_corpus, provider)

    everything = store_.search(query, "chunk", provider.name, pg_corpus["user_id"], top_k=50)
    assert len(everything) >= 3

    assert len(store_.search(query, "chunk", provider.name, pg_corpus["user_id"], top_k=2)) == 2

    dropped = everything[0][0]
    remaining = store_.search(
        query, "chunk", provider.name, pg_corpus["user_id"], top_k=50, exclude_ids=[dropped]
    )
    assert dropped not in [oid for oid, _ in remaining]
    assert len(remaining) == len(everything) - 1


@requires_postgres
def test_native_search_does_not_cross_users(pg_conn, provider, config):
    """The join is the isolation boundary. This is the test that says so.

    Both users get a note on the same topic, so a missing scope predicate ranks
    the other user's chunks highly rather than harmlessly returning nothing.
    """
    from studylink.vectorstore import VectorStore

    alice = store.create_user(pg_conn, email="alice@example.edu")
    bob = store.create_user(pg_conn, email="bob@example.edu")
    for user, marker in [(alice, "ALICE-SECRET-TOKEN"), (bob, "BOB-SECRET-TOKEN")]:
        course = store.upsert_course(pg_conn, "101", "CS 101", "CS101", user_id=user)
        store.create_note(
            pg_conn,
            f"{marker} notes",
            "Gradient descent steps downhill; the learning rate alpha controls "
            f"the step size. {marker}",
            course_id=course,
            user_id=user,
        )
        Indexer(pg_conn, provider, config, user).reindex()

    store_ = VectorStore(pg_conn)
    query = provider.embed(["gradient descent learning rate"])[0]

    alice_hits = {oid for oid, _ in store_.search(query, "chunk", provider.name, alice, top_k=50)}
    bob_hits = {oid for oid, _ in store_.search(query, "chunk", provider.name, bob, top_k=50)}

    assert alice_hits and bob_hits
    assert alice_hits.isdisjoint(bob_hits)


@requires_postgres
def test_native_search_reports_a_dimension_mismatch_clearly(pg_corpus, provider):
    """Changing embedding model without reindexing must not surface as SQL noise."""
    from studylink.vectorstore import VectorStore

    store_ = VectorStore(pg_corpus["conn"])
    wrong_width = np.ones(configured_dim() + 8, dtype=np.float32)

    with pytest.raises(ValueError, match="Re-index after changing embedding models"):
        store_.search(wrong_width, "chunk", provider.name, pg_corpus["user_id"], top_k=5)

    # The connection has to survive the failure, not stay in an aborted transaction.
    assert store_.count("chunk", provider.name, pg_corpus["user_id"]) > 0


@requires_postgres
def test_backfill_fills_rows_written_before_the_column_existed(pg_corpus, provider):
    """Simulates migrating a database that already had embeddings.

    A half-filled column is the dangerous state: search would return a subset of
    the corpus and look like it worked.
    """
    from studylink.pgvector_support import backfill_native_vectors

    conn = pg_corpus["conn"]
    conn.execute(text("UPDATE embeddings SET embedding = NULL"))
    conn.commit()

    missing = conn.execute(
        text("SELECT count(*) FROM embeddings WHERE embedding IS NULL")
    ).scalar()
    assert missing > 0

    assert backfill_native_vectors(conn) == missing
    assert conn.execute(
        text("SELECT count(*) FROM embeddings WHERE embedding IS NULL")
    ).scalar() == 0

    # And the backfilled vectors must equal the blobs they came from.
    for blob, native in conn.execute(
        text("SELECT vector, embedding::text FROM embeddings")
    ).all():
        from_blob = np.frombuffer(blob, dtype=np.float32)
        from_native = np.array(
            [float(x) for x in native.strip("[]").split(",")], dtype=np.float32
        )
        assert np.allclose(from_blob, from_native, atol=1e-6)

    assert backfill_native_vectors(conn) == 0  # idempotent


# ----------------------------------------------------------------- hnsw index


def test_index_helpers_are_no_ops_on_sqlite(conn):
    from studylink.pgvector_support import (
        apply_search_tuning,
        ensure_hnsw_index,
        has_hnsw_index,
    )

    assert has_hnsw_index(conn) is False
    assert ensure_hnsw_index(conn) is False
    apply_search_tuning(conn)  # must not raise


@pytest.fixture
def hnsw_conn(pg_conn, monkeypatch):
    """A connection with the opt-in index actually built.

    The index is off by default because it costs recall on a multi-tenant table
    (see `hnsw_enabled`), but the machinery still has to be correct for anyone who
    turns it on -- so the tests below build it explicitly.
    """
    from studylink.pgvector_support import ensure_hnsw_index

    monkeypatch.setenv("STUDYLINK_HNSW", "1")
    assert ensure_hnsw_index(pg_conn) is True
    yield pg_conn
    # `reset` truncates rows but leaves schema objects, and the test database is
    # shared across the module -- without this the index outlives the test that
    # asked for it and the default-off assertions start failing.
    pg_conn.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX_NAME}"))
    pg_conn.commit()


@requires_postgres
def test_hnsw_index_is_off_by_default(pg_conn):
    """Default-on would silently cost recall as soon as a corpus grows."""
    from studylink.pgvector_support import ensure_hnsw_index, has_hnsw_index, hnsw_enabled

    assert hnsw_enabled() is False
    assert ensure_hnsw_index(pg_conn) is False
    assert has_hnsw_index(pg_conn) is False


@requires_postgres
def test_hnsw_index_can_be_enabled_and_is_idempotent(hnsw_conn):
    from studylink.pgvector_support import ensure_hnsw_index, has_hnsw_index

    assert has_hnsw_index(hnsw_conn) is True
    assert ensure_hnsw_index(hnsw_conn) is True  # second call changes nothing
    assert has_hnsw_index(hnsw_conn) is True


@requires_postgres
def test_search_is_exact_without_the_index(pg_corpus, provider):
    """The reason the index is off: no index means no approximation.

    Weak as an assertion on a small corpus, but it is the property the default
    exists to preserve, and it fails loudly if search ever starts approximating
    without anyone opting in.
    """
    from studylink.pgvector_support import has_hnsw_index
    from studylink.vectorstore import VectorStore

    assert has_hnsw_index(pg_corpus["conn"]) is False

    store_ = VectorStore(pg_corpus["conn"])
    query = _query_vector(pg_corpus, provider)
    native = store_.search(query, "chunk", provider.name, pg_corpus["user_id"], top_k=10)
    exact = store_._search_numpy(query, "chunk", provider.name, pg_corpus["user_id"], 10, ())

    assert [oid for oid, _ in native] == [oid for oid, _ in exact]


@requires_postgres
def test_hnsw_index_uses_the_cosine_operator_class(hnsw_conn):
    """The opclass has to match the operator the search uses.

    Build the index with `vector_l2_ops` and query with `<=>` and Postgres simply
    never uses it: the rows still come back correct, just from a full sort. That
    is a performance regression with no failing test and no error message
    anywhere, so it gets one here.
    """
    opclass = hnsw_conn.execute(
        text(
            "SELECT o.opcname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_opclass o ON o.oid = i.indclass[0] "
            "WHERE c.relname = :name"
        ),
        {"name": HNSW_INDEX_NAME},
    ).scalar()
    assert opclass == "vector_cosine_ops"


@requires_postgres
def test_planner_can_reach_the_hnsw_index(hnsw_conn, provider, config):
    """A nearest-neighbour query on the bare table must be able to use the index.

    Deliberately not asserted against the query `_search_native` builds: that one
    joins to the owner table for tenant scoping and the planner will not use the
    index underneath the join. This asserts the index itself is well-formed and
    reachable, which is the part that would otherwise break silently.
    """
    user_id = store.get_or_create_default_user(hnsw_conn)
    for i in range(20):
        store.create_note(hnsw_conn, f"Note {i}", f"Topic number {i} about learning.", user_id=user_id)
    Indexer(hnsw_conn, provider, config, user_id).reindex()
    hnsw_conn.execute(text("ANALYZE embeddings"))

    query = to_pgvector_literal(provider.embed(["learning"])[0])
    # The table is tiny, so a sequential scan genuinely is cheaper; disable it to
    # ask the narrower question of whether the index *can* serve this query.
    hnsw_conn.execute(text("SET enable_seqscan = off"))
    try:
        plan = "\n".join(
            row[0]
            for row in hnsw_conn.execute(
                text(
                    "EXPLAIN SELECT owner_id FROM embeddings "
                    "ORDER BY embedding <=> :q LIMIT 5"
                ),
                {"q": query},
            )
        )
    finally:
        hnsw_conn.execute(text("SET enable_seqscan = on"))

    assert HNSW_INDEX_NAME in plan, plan


@requires_postgres
def test_connections_carry_the_configured_ef_search(pg_conn):
    """It is a per-session GUC, so it has to be set on every connection."""
    from studylink.pgvector_support import configured_ef_search

    assert int(pg_conn.execute(text("SHOW hnsw.ef_search")).scalar()) == configured_ef_search()


@requires_postgres
def test_ef_search_leaves_room_above_top_k(pg_corpus, provider):
    """ef_search below the LIMIT caps how many rows HNSW can return at all.

    The tenant predicate discards candidates after the index produces them, so
    the margin has to cover that too -- this asserts the default is not merely
    equal to a typical top_k.
    """
    from studylink.pgvector_support import configured_ef_search

    assert configured_ef_search() >= 10 * config_top_k()


def config_top_k() -> int:
    from studylink.config import RetrievalConfig

    return RetrievalConfig().top_k


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
