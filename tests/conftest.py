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
def corpus(conn, provider, config):
    """A tiny two-course corpus with one obvious match per assignment."""
    cs = store.upsert_course(conn, "1", "CS 101: Machine Learning", "CS101")
    hist = store.upsert_course(conn, "2", "HIST 200: European History", "HIST200")

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

    Indexer(conn, provider, config).reindex()

    return {
        "conn": conn,
        "retriever": Retriever(conn, provider, config),
        "cs": cs,
        "hist": hist,
        "gradient_assignment": gradient_assignment,
        "essay_assignment": essay_assignment,
        "gradient_note": gradient_note,
        "alliance_note": alliance_note,
        "unrelated_note": unrelated_note,
    }
