"""Two users, one database: nothing of A's may reach B.

These are the tests that make the rest of day 1 mean something. Each one deletes
a specific hole:

  * a scoped list query that forgot its WHERE clause
  * a vector search that scanned the whole embeddings table
  * a caller-supplied id that was trusted without an ownership check
  * a UNIQUE constraint that collapsed two users' Canvas courses into one row

Every one of these has shipped in a real product at some point. They are cheap
to test and expensive to discover in production.
"""

from __future__ import annotations

import pytest

from studylink import store
from studylink.config import RetrievalConfig
from studylink.embeddings import build_provider
from studylink.errors import CrossUserAccessError, NotFoundError, assert_owned
from studylink.indexing import Indexer
from studylink.retrieval import Retriever


@pytest.fixture
def two_users(conn, provider, config):
    """Alice and Bob, each with a course, an assignment, and a note.

    Their content is deliberately on the *same topic*: if scoping is broken,
    retrieval will happily rank the other user's note first, and the assertions
    below will catch it. Two users with unrelated corpora would pass by luck.
    """
    alice = store.create_user(conn, email="alice@example.edu")
    bob = store.create_user(conn, email="bob@example.edu")

    alice_course = store.upsert_course(conn, "101", "CS 101", "CS101", user_id=alice)
    bob_course = store.upsert_course(conn, "101", "CS 101", "CS101", user_id=bob)

    alice_assignment = store.upsert_assignment(
        conn,
        course_id=alice_course,
        canvas_id="900",
        name="Problem Set: Gradient Descent",
        description="Implement gradient descent and compare learning rates.",
        user_id=alice,
    )
    bob_assignment = store.upsert_assignment(
        conn,
        course_id=bob_course,
        canvas_id="900",
        name="Problem Set: Gradient Descent",
        description="Implement gradient descent and compare learning rates.",
        user_id=bob,
    )

    alice_note = store.create_note(
        conn,
        "Alice's gradient descent notes",
        "Gradient descent steps downhill. The learning rate alpha controls step size; "
        "too large and the loss diverges. ALICE-SECRET-TOKEN",
        course_id=alice_course,
        user_id=alice,
    )
    bob_note = store.create_note(
        conn,
        "Bob's gradient descent notes",
        "Gradient descent steps downhill. The learning rate alpha controls step size; "
        "too large and the loss diverges. BOB-SECRET-TOKEN",
        course_id=bob_course,
        user_id=bob,
    )

    Indexer(conn, provider, config, alice).reindex()
    Indexer(conn, provider, config, bob).reindex()

    return {
        "alice": alice,
        "bob": bob,
        "alice_course": alice_course,
        "bob_course": bob_course,
        "alice_assignment": alice_assignment,
        "bob_assignment": bob_assignment,
        "alice_note": alice_note,
        "bob_note": bob_note,
    }


# ------------------------------------------------------------------ store layer


def test_list_notes_is_scoped(conn, two_users):
    alice_notes = store.list_notes(conn, two_users["alice"])
    bob_notes = store.list_notes(conn, two_users["bob"])

    assert [n.id for n in alice_notes] == [two_users["alice_note"]]
    assert [n.id for n in bob_notes] == [two_users["bob_note"]]
    assert "BOB-SECRET-TOKEN" not in " ".join(n.body for n in alice_notes)


def test_get_note_returns_nothing_for_the_wrong_user(conn, two_users):
    assert store.get_note(conn, two_users["alice_note"], two_users["alice"]) is not None
    assert store.get_note(conn, two_users["alice_note"], two_users["bob"]) is None


def test_get_notes_batch_filters_foreign_ids(conn, two_users):
    both = [two_users["alice_note"], two_users["bob_note"]]
    fetched = store.get_notes(conn, both, two_users["alice"])
    assert list(fetched) == [two_users["alice_note"]]


def test_courses_and_assignments_are_scoped(conn, two_users):
    assert [c.id for c in store.list_courses(conn, two_users["alice"])] == [
        two_users["alice_course"]
    ]
    assert [a.id for a in store.list_assignments(conn, two_users["bob"])] == [
        two_users["bob_assignment"]
    ]


def test_same_canvas_id_is_two_rows_for_two_users(conn, two_users):
    """Both students are in Canvas course 101; that must not be one shared row."""
    assert two_users["alice_course"] != two_users["bob_course"]

    # Re-syncing is still idempotent per user -- no duplicate rows.
    again = store.upsert_course(conn, "101", "CS 101", "CS101", user_id=two_users["alice"])
    assert again == two_users["alice_course"]
    assert len(store.list_courses(conn, two_users["alice"])) == 1


def test_chunks_inherit_their_note_owner(conn, two_users):
    alice_chunks = store.list_chunks(conn, two_users["alice"])
    assert alice_chunks
    assert all(
        store.get_note(conn, c.note_id, two_users["alice"]) is not None
        for c in alice_chunks
    )
    bob_chunk_ids = {c.id for c in store.list_chunks(conn, two_users["bob"])}
    assert bob_chunk_ids.isdisjoint({c.id for c in alice_chunks})


# -------------------------------------------------------------------- retrieval


def test_retrieval_never_crosses_users(conn, provider, config, two_users):
    """The sharpest test here: identical topics, so only scoping separates them."""
    alice_retriever = Retriever(conn, provider, config, two_users["alice"])
    assignment = store.get_assignment(
        conn, two_users["alice_assignment"], two_users["alice"]
    )

    matches = alice_retriever.notes_for_assignment(assignment, apply_threshold=False)

    assert matches, "Alice should match her own note"
    returned = {m.note.id for m in matches}
    assert returned == {two_users["alice_note"]}
    assert two_users["bob_note"] not in returned
    assert all("BOB-SECRET-TOKEN" not in m.note.body for m in matches)


def test_free_text_search_is_scoped(conn, provider, config, two_users):
    bob_retriever = Retriever(conn, provider, config, two_users["bob"])
    matches = bob_retriever.search_notes("learning rate", top_k=10)

    assert {m.note.id for m in matches} == {two_users["bob_note"]}


def test_reverse_lookup_is_scoped(conn, provider, config, two_users):
    alice_retriever = Retriever(conn, provider, config, two_users["alice"])
    note = store.get_note(conn, two_users["alice_note"], two_users["alice"])

    results = alice_retriever.assignments_for_note(note, top_k=10, apply_threshold=False)

    assert {m.assignment.id for m in results} == {two_users["alice_assignment"]}


def test_vector_search_is_scoped(conn, provider, config, two_users):
    """Both users embedded near-identical text, so the vectors are near-identical."""
    retriever = Retriever(conn, provider, config, two_users["alice"])
    query = provider.embed(["gradient descent learning rate"])[0]

    hits = retriever.vectors.search(query, "chunk", provider.name, two_users["alice"], top_k=50)

    alice_chunk_ids = {c.id for c in store.list_chunks(conn, two_users["alice"])}
    assert hits
    assert {chunk_id for chunk_id, _ in hits} <= alice_chunk_ids


def test_vector_count_is_per_user(conn, provider, config, two_users):
    retriever = Retriever(conn, provider, config, two_users["alice"])
    alice_n = retriever.vectors.count("chunk", provider.name, two_users["alice"])
    bob_n = retriever.vectors.count("chunk", provider.name, two_users["bob"])
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE owner_type = 'chunk'"
    ).fetchone()["n"]

    assert alice_n > 0 and bob_n > 0
    assert alice_n + bob_n == total


# ----------------------------------------------------------------- ownership


def test_assert_owned_raises_for_a_foreign_row(conn, two_users):
    with pytest.raises(CrossUserAccessError) as caught:
        assert_owned(conn, "notes", two_users["alice_note"], two_users["bob"])

    error = caught.value
    assert error.owner_id == two_users["alice"]
    assert error.actor_id == two_users["bob"]


def test_assert_owned_distinguishes_missing_from_foreign(conn, two_users):
    with pytest.raises(NotFoundError):
        assert_owned(conn, "notes", 999_999, two_users["alice"])


def test_assert_owned_rejects_unknown_tables(conn, two_users):
    with pytest.raises(ValueError):
        assert_owned(conn, "embeddings", 1, two_users["alice"])


def test_deleting_a_user_takes_their_data_with_them(conn, two_users):
    conn.execute("DELETE FROM users WHERE id = ?", (two_users["alice"],))
    conn.commit()

    assert store.list_notes(conn, two_users["alice"]) == []
    assert store.list_courses(conn, two_users["alice"]) == []
    # Bob is untouched.
    assert len(store.list_notes(conn, two_users["bob"])) == 1


def test_indexing_one_user_does_not_touch_another(conn, provider, two_users):
    """Re-chunking Alice at a new size must leave Bob's chunks alone."""
    bob_before = {(c.id, c.text) for c in store.list_chunks(conn, two_users["bob"])}

    narrow = RetrievalConfig(chunk_size=20, chunk_overlap=5, top_k=3, score_threshold=0.05)
    Indexer(conn, provider, narrow, two_users["alice"]).reindex()

    bob_after = {(c.id, c.text) for c in store.list_chunks(conn, two_users["bob"])}
    assert bob_before == bob_after
