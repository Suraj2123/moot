from __future__ import annotations

import numpy as np

from studylink import store
from studylink.config import RetrievalConfig
from studylink.embeddings import build_provider
from studylink.retrieval import calibrate_confidence, explain
from studylink.vectorstore import VectorStore


def test_assignment_retrieves_its_own_course_notes_first(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["gradient_assignment"], corpus["user_id"])
    matches = corpus["retriever"].notes_for_assignment(assignment, apply_threshold=False)

    assert matches, "expected at least one match"
    assert matches[0].note.id == corpus["gradient_note"]


def test_history_assignment_does_not_rank_the_ml_note_first(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["essay_assignment"], corpus["user_id"])
    matches = corpus["retriever"].notes_for_assignment(assignment, apply_threshold=False)

    assert matches[0].note.id == corpus["alliance_note"]
    top_ids = [m.note.id for m in matches[:1]]
    assert corpus["gradient_note"] not in top_ids


def test_reverse_lookup_finds_the_right_assignment(corpus):
    note = store.get_note(corpus["conn"], corpus["alliance_note"], corpus["user_id"])
    matches = corpus["retriever"].assignments_for_note(note, top_k=3, apply_threshold=False)

    assert matches
    assert matches[0].assignment.id == corpus["essay_assignment"]


def test_matches_carry_evidence(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["gradient_assignment"], corpus["user_id"])
    match = corpus["retriever"].notes_for_assignment(assignment, apply_threshold=False)[0]

    assert match.evidence.snippet
    assert "gradient" in match.evidence.overlapping_terms
    assert match.matched_chunk is not None


def test_threshold_filters_weak_matches(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["gradient_assignment"], corpus["user_id"])
    retriever = corpus["retriever"]

    unfiltered = retriever.notes_for_assignment(assignment, top_k=10, apply_threshold=False)
    retriever.config = RetrievalConfig(
        chunk_size=60, chunk_overlap=10, top_k=10, score_threshold=0.99
    )
    filtered = retriever.notes_for_assignment(assignment, top_k=10, apply_threshold=True)

    assert len(unfiltered) > len(filtered)


def test_free_text_search(corpus):
    matches = corpus["retriever"].search_notes("learning rate convergence", top_k=3)
    assert matches
    assert matches[0].note.id == corpus["gradient_note"]


def test_empty_query_returns_nothing(corpus):
    assert corpus["retriever"].search_notes("   ") == []


def test_explain_reports_shared_terms_only():
    evidence = explain(
        "gradient descent learning rate convergence",
        "The learning rate controls convergence. Random forests are unrelated.",
    )
    assert "learning" in evidence.overlapping_terms
    assert "forest" not in evidence.overlapping_terms
    assert "convergence" in evidence.snippet


def test_explain_handles_no_overlap():
    evidence = explain("gradient descent", "The Marshall Plan transferred aid to Europe.")
    assert evidence.overlapping_terms == []
    assert evidence.snippet  # still shows something the student can read


def test_confidence_is_bounded_and_monotonic():
    config = RetrievalConfig(score_threshold=0.1, confidence_ceiling=0.6)
    evidence = explain("gradient descent learning", "gradient descent learning rate")

    low = calibrate_confidence(0.05, evidence, "gradient descent learning", config)
    mid = calibrate_confidence(0.35, evidence, "gradient descent learning", config)
    high = calibrate_confidence(0.90, evidence, "gradient descent learning", config)

    assert 0.0 <= low <= mid <= high <= 1.0


def test_vector_store_roundtrip_and_dimension_guard(conn):
    provider = build_provider("hash", "hash-64")
    vectors = VectorStore(conn)
    matrix = provider.embed(["alpha beta", "gamma delta"])
    vectors.upsert_many("chunk", [1, 2], matrix, provider.name)

    ids, loaded = vectors.matrix("chunk", provider.name)
    assert ids == [1, 2]
    assert loaded.shape == (2, 64)
    assert np.allclose(loaded, matrix)

    hits = vectors.search(matrix[0], "chunk", provider.name, top_k=2)
    assert hits[0][0] == 1
    assert hits[0][1] > hits[1][1]


def test_vector_store_rejects_dimension_mismatch(conn):
    small = build_provider("hash", "hash-64")
    vectors = VectorStore(conn)
    vectors.upsert_many("chunk", [1], small.embed(["alpha"]), small.name)

    big = build_provider("hash", "hash-256")
    try:
        vectors.search(big.embed(["alpha"])[0], "chunk", small.name, top_k=1)
    except ValueError as exc:
        assert "Dimension mismatch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a dimension mismatch error")
