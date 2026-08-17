from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink import store  # noqa: E402
from studylink.config import RetrievalConfig  # noqa: E402
from studylink.db import connect  # noqa: E402
from studylink.embeddings import build_provider  # noqa: E402
from studylink.indexing import Indexer  # noqa: E402
from studylink.retrieval import Retriever  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def provider():
    # The hash provider keeps tests offline and deterministic.
    return build_provider("hash", "hash-256")


@pytest.fixture
def config():
    return RetrievalConfig(chunk_size=60, chunk_overlap=10, top_k=3, score_threshold=0.05)


@pytest.fixture
def user_id(conn):
    """The single local user. Most tests only need one owner."""
    return store.get_or_create_default_user(conn)


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


@pytest.fixture
def corpus(conn, provider, config, user_id):
    """A tiny two-course corpus with one obvious match per assignment."""
    cs = store.upsert_course(conn, "1", "CS 101: Machine Learning", "CS101", user_id=user_id)
    hist = store.upsert_course(conn, "2", "HIST 200: European History", "HIST200", user_id=user_id)

    gradient_assignment = store.upsert_assignment(
        conn,
        course_id=cs,
        canvas_id="10",
        name="Problem Set: Gradient Descent",
        description="Implement gradient descent and compare learning rates for convergence.",
    )
    essay_assignment = store.upsert_assignment(
        conn,
        course_id=hist,
        canvas_id="11",
        name="Essay: Causes of the First World War",
        description="Argue for the most important cause of the outbreak of war in 1914, "
        "engaging with the alliance system and the July crisis.",
    )

    gradient_note = store.create_note(
        conn,
        "Lecture on gradient descent",
        "Gradient descent minimises a loss by stepping downhill. The learning rate alpha "
        "controls the step size: too small and convergence is slow, too large and the loss "
        "diverges. Stochastic gradient descent updates per example and converges faster on "
        "large datasets.",
        course_id=cs,
    )
    alliance_note = store.create_note(
        conn,
        "Lecture on the alliance system",
        "By 1907 Europe had split into two blocs. The Triple Alliance faced the Triple "
        "Entente, and the July crisis of 1914 turned a Balkan quarrel into a continental war "
        "because mobilisation schedules removed the time for diplomacy.",
        course_id=hist,
    )
    unrelated_note = store.create_note(
        conn,
        "Lecture on postwar reconstruction",
        "The Marshall Plan transferred aid to western Europe after 1945 and tied recipient "
        "economies to the United States, accelerating the division of the continent.",
        course_id=hist,
    )

    Indexer(conn, provider, config, user_id).reindex()

    return {
        "conn": conn,
        "user_id": user_id,
        "retriever": Retriever(conn, provider, config, user_id),
        "cs": cs,
        "hist": hist,
        "gradient_assignment": gradient_assignment,
        "essay_assignment": essay_assignment,
        "gradient_note": gradient_note,
        "alliance_note": alliance_note,
        "unrelated_note": unrelated_note,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient against a throwaway database.

    The API's engine is a module global built on first use, so it has to be
    reset around each test or the first test's database serves all of them.
    The rate limiter is process-wide for the same reason.
    """
    from fastapi.testclient import TestClient

    from studylink import api as api_module
    from studylink import ratelimit

    monkeypatch.setenv("STUDYLINK_DB", str(tmp_path / "api.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    api_module._engine = None
    ratelimit.limiter.reset()

    with TestClient(api_module.api) as test_client:
        yield test_client

    api_module._engine = None
    ratelimit.limiter.reset()


@pytest.fixture
def signup():
    """Register an account through the API and return its bearer token."""

    def _signup(client, email="alice@school.edu", password="correct horse battery"):
        response = client.post(
            "/auth/signup", json={"email": email, "password": password}
        )
        assert response.status_code == 201, response.text
        return response.json()["token"]

    return _signup
