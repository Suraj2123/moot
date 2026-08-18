"""The groundedness eval -- tests of the measurement, not of the model.

A metric that cannot distinguish a good system from an obviously bad one is
worse than no metric, because it gets quoted. So each of these builds a
deliberately broken chat and checks the number moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studylink import store
from studylink.chat import Answer, NoteChat
from studylink.evaluation import groundedness as g
from studylink.indexing import Indexer
from studylink.retrieval import Retriever
from tests.test_chat import FakeClient


@pytest.fixture
def corpus(conn, provider, config, user_id):
    store.create_note(
        conn, "Gradient descent",
        "Gradient descent minimises a loss by stepping downhill. The learning "
        "rate alpha controls the step size: too large and the loss diverges.",
        user_id=user_id,
    )
    Indexer(conn, provider, config, user_id).reindex()
    return {"conn": conn, "user_id": user_id}


def make_chat(corpus, provider, config, reply):
    return NoteChat(
        corpus["conn"],
        Retriever(corpus["conn"], provider, config, corpus["user_id"]),
        user_id=corpus["user_id"],
        client=FakeClient(reply),
    )


def test_the_shipped_case_file_loads_and_has_both_kinds():
    cases = g.load_cases(Path("data/eval/groundedness.json"))

    assert len(cases) >= 6
    assert any(c.answerable for c in cases)
    assert any(not c.answerable for c in cases), (
        "without unanswerable cases, refusal_accuracy is undefined and the "
        "other metrics are trivially gamed"
    )


def test_claims_are_sentences_long_enough_to_be_claims():
    text = "Yes. Gradient descent minimises a loss by stepping downhill [N1]."
    claims = g.split_claims(text)

    assert len(claims) == 1  # "Yes." is not a claim
    assert "[N1]" in claims[0]


def test_a_perfect_answer_scores_well(corpus, provider, config):
    chat = make_chat(
        corpus, provider, config,
        "Gradient descent minimises a loss by stepping downhill [N1].",
    )
    cases = [g.GroundednessCase("How does gradient descent work?", answerable=True)]

    report = g.evaluate(chat, cases)

    assert report.citation_validity == 1.0
    assert report.citation_coverage == 1.0
    assert report.answer_rate == 1.0


def test_invented_citations_drag_validity_down(corpus, provider, config):
    chat = make_chat(
        corpus, provider, config,
        "Gradient descent steps downhill [N1]. It was also covered in lecture [N999].",
    )
    cases = [g.GroundednessCase("How does gradient descent work?", answerable=True)]

    report = g.evaluate(chat, cases)

    assert report.citation_validity < 1.0
    assert report.as_dict()["invented_citations"] == 1


def test_uncited_claims_drag_coverage_down(corpus, provider, config):
    """An answer with one citation and six paragraphs is decorated, not
    grounded."""
    chat = make_chat(
        corpus, provider, config,
        "Gradient descent minimises a loss by stepping downhill [N1]. "
        "It is also widely used in modern deep learning systems everywhere. "
        "Most practitioners tune the learning rate by hand over many runs.",
    )
    cases = [g.GroundednessCase("How does gradient descent work?", answerable=True)]

    report = g.evaluate(chat, cases)

    assert report.citation_validity == 1.0  # nothing invented
    assert report.citation_coverage < 0.5  # but most claims carry nothing


def test_a_system_that_answers_everything_scores_zero_on_refusal(corpus, provider, config):
    """The metric that catches confident nonsense, which the other two miss."""
    chat = make_chat(corpus, provider, config, "Certainly, it is 3422 degrees [N1].")

    # Force a match so the model is actually called for an unanswerable question.
    cases = [g.GroundednessCase("gradient descent melting point", answerable=False)]
    report = g.evaluate(chat, cases)

    assert report.refusal_accuracy == 0.0
    assert report.citation_validity == 1.0, (
        "citation metrics alone would call this system perfectly grounded"
    )


def test_a_system_that_refuses_everything_scores_zero_on_answer_rate(corpus, provider, config):
    """The counterweight. Refusing everything is not grounding, it is silence."""
    chat = make_chat(corpus, provider, config, "anything")
    cases = [
        g.GroundednessCase("what is the melting point of tungsten?", answerable=True),
    ]

    report = g.evaluate(chat, cases)

    assert report.answer_rate == 0.0
    assert report.refusal_accuracy == 0.0  # no unanswerable cases to judge


def test_correct_refusal_scores_full_marks(corpus, provider, config):
    chat = make_chat(corpus, provider, config, "unused")
    cases = [g.GroundednessCase("what is the melting point of tungsten?", answerable=False)]

    report = g.evaluate(chat, cases)

    assert report.refusal_accuracy == 1.0


def test_the_report_formats(corpus, provider, config):
    chat = make_chat(corpus, provider, config, "Gradient descent steps downhill [N1].")
    cases = g.load_cases(Path("data/eval/groundedness.json"))[:2]

    text = g.format_report(g.evaluate(chat, cases))

    assert "citation validity" in text
    assert "refusal accuracy" in text
