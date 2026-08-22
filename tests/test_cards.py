"""Flashcards: generation, grounding, scheduling, and practice tests.

Only generation touches a model, and it is mocked at the client boundary. The
scheduler and the test builder are deterministic by design, which is most of
why they are separate from generation at all -- the parts a student relies on
every day should not depend on a network call.

The test that matters most is the grounding one. A flashcard that teaches
something the notes never said is worse than no flashcard: the student will
study it, believe it, and be confidently wrong in an exam.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from studylink import cards


NOTE = (
    "Gradient descent minimises a loss function by stepping downhill. "
    "The learning rate alpha controls the size of each step. "
    "Too large an alpha overshoots the minimum and diverges."
)


class FakeMessage:
    def __init__(self, text, input_tokens=100, output_tokens=200):
        self.content = [type("Block", (), {"text": text})()]
        self.usage = type("U", (), {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        })()


class FakeClient:
    """Stands in for anthropic.Anthropic at the one place it is used."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return FakeMessage(outer.reply)

        self.messages = Messages()


def reply_with(*pairs) -> str:
    import json

    return json.dumps({
        "cards": [
            {"front": f, "back": b, "evidence": e} for f, b, e in pairs
        ]
    })


# ------------------------------------------------------------------ grounding


def test_a_card_quoting_the_note_is_kept():
    writer = cards.CardWriter(client=FakeClient(reply_with(
        ("What does the learning rate control?", "The size of each step",
         "The learning rate alpha controls the size of each step."),
    )))

    result = writer.write("Lecture 4", NOTE)

    assert len(result.cards) == 1
    assert result.rejected == 0
    assert result.cards[0].back == "The size of each step"


def test_a_card_the_note_does_not_support_is_dropped():
    """The whole point. A model asked for an exact quote usually gives one;
    when it does not, that card is precisely the one that invented something."""
    writer = cards.CardWriter(client=FakeClient(reply_with(
        ("What does alpha control?", "Step size",
         "The learning rate alpha controls the size of each step."),
        ("Who invented gradient descent?", "Cauchy, in 1847",
         "Cauchy first described the method in 1847."),   # not in the note
    )))

    result = writer.write("Lecture 4", NOTE)

    assert [c.back for c in result.cards] == ["Step size"]
    assert result.rejected == 1


def test_a_card_with_no_evidence_at_all_is_dropped():
    writer = cards.CardWriter(client=FakeClient(reply_with(
        ("Something?", "Something", ""),
    )))
    assert writer.write("Lecture 4", NOTE).cards == []


def test_evidence_matching_survives_rewrapped_whitespace():
    """PDF extraction re-wraps lines, so an otherwise perfect quote can differ
    from the note by a newline. Rejecting on that drops good cards."""
    writer = cards.CardWriter(client=FakeClient(reply_with(
        ("Q?", "A", "The learning rate alpha\n   controls the size of each step."),
    )))
    assert len(writer.write("Lecture 4", NOTE).cards) == 1


def test_usage_is_reported_for_metering():
    writer = cards.CardWriter(client=FakeClient(reply_with(("Q?", "A", NOTE[:40]))))
    result = writer.write("Lecture 4", NOTE)
    assert result.usage == {"input_tokens": 100, "output_tokens": 200}


def test_a_note_too_short_to_be_worth_carding_is_refused():
    writer = cards.CardWriter(client=FakeClient(reply_with(("Q?", "A", "x"))))
    with pytest.raises(cards.CardError, match="too short"):
        writer.write("Stub", "Not much here.")


def test_the_requested_count_is_capped():
    writer = cards.CardWriter(client=FakeClient(reply_with(("Q?", "A", NOTE[:40]))))
    writer.write("Lecture 4", NOTE, count=500)
    prompt = writer._client.calls[0]["messages"][0]["content"]
    assert f"up to {cards.MAX_CARDS_PER_NOTE} flashcards" in prompt


# -------------------------------------------------------------------- parsing


def test_json_fenced_in_markdown_still_parses():
    """Models do this often enough that failing on it is choosing brittleness."""
    text = '```json\n{"cards": [{"front": "Q", "back": "A", "evidence": "E"}]}\n```'
    assert [c.front for c in cards.parse_cards(text)] == ["Q"]


def test_prose_around_the_json_still_parses():
    text = 'Here are your cards:\n{"cards": [{"front": "Q", "back": "A", "evidence": "E"}]}\nHope that helps!'
    assert len(cards.parse_cards(text)) == 1


def test_unparseable_output_yields_no_cards_rather_than_raising():
    assert cards.parse_cards("I'm sorry, I can't do that.") == []
    assert cards.parse_cards("") == []


def test_a_card_missing_a_side_is_skipped():
    text = '{"cards": [{"front": "", "back": "A"}, {"front": "Q", "back": "A", "evidence": "E"}]}'
    assert len(cards.parse_cards(text)) == 1


# ----------------------------------------------------------------- scheduling


def test_a_new_card_answered_well_comes_back_tomorrow():
    interval, _, _ = cards.schedule(cards.GOOD, interval_days=0, ease=250, reviews=0)
    assert interval == 1


def test_the_second_success_jumps_to_six_days():
    """Multiplying from one would step 1 -> 2 -> 5, which is more reviews than
    the recall data justifies."""
    interval, _, _ = cards.schedule(cards.GOOD, interval_days=1, ease=250, reviews=1)
    assert interval == 6


def test_intervals_grow_by_ease_after_that():
    interval, _, _ = cards.schedule(cards.GOOD, interval_days=6, ease=250, reviews=2)
    assert interval == 15  # 6 * 2.5


def test_forgetting_resets_to_tomorrow_and_costs_ease():
    interval, ease, due = cards.schedule(
        cards.FORGOT, interval_days=30, ease=250, reviews=9
    )
    assert interval == 1
    assert ease == 230
    assert (due - datetime.now(timezone.utc)).days == 0  # tomorrow, not in ten minutes


def test_easy_grows_faster_than_good_which_grows_faster_than_hard():
    hard, _, _ = cards.schedule(cards.HARD, interval_days=10, ease=250, reviews=5)
    good, _, _ = cards.schedule(cards.GOOD, interval_days=10, ease=250, reviews=5)
    easy, _, _ = cards.schedule(cards.EASY, interval_days=10, ease=250, reviews=5)
    assert hard < good < easy


def test_ease_has_a_floor():
    """Without one, a card the student keeps failing decays toward a multiplier
    of ~1 and is shown forever at the same interval -- punishment, not learning."""
    ease = 250
    for _ in range(50):
        _, ease, _ = cards.schedule(cards.FORGOT, interval_days=1, ease=ease, reviews=1)
    assert ease == cards.MINIMUM_EASE


def test_an_interval_never_reaches_zero():
    interval, _, _ = cards.schedule(cards.HARD, interval_days=1, ease=130, reviews=5)
    assert interval >= 1


def test_an_unknown_grade_is_refused():
    with pytest.raises(cards.CardError):
        cards.schedule(99, interval_days=1, ease=250, reviews=1)


# -------------------------------------------------------------- practice tests


def deck(n: int) -> list[dict]:
    return [{"id": i, "front": f"Q{i}", "back": f"A{i}"} for i in range(1, n + 1)]


def test_multiple_choice_includes_the_right_answer():
    questions = cards.build_test(deck(10), rng=random.Random(0))
    assert all(q.answer in q.choices for q in questions)


def test_distractors_come_from_the_same_deck():
    """Wrong options drawn from the same lecture are genuinely confusable;
    invented ones are usually obviously wrong."""
    backs = {c["back"] for c in deck(10)}
    for question in cards.build_test(deck(10), rng=random.Random(1)):
        assert set(question.choices) <= backs


def test_a_question_never_offers_its_answer_twice():
    for question in cards.build_test(deck(10), rng=random.Random(2)):
        assert len(question.choices) == len(set(question.choices))


def test_a_small_deck_falls_back_to_written():
    """Three cards cannot make a four-option question, and padding with
    nonsense would make the test easier rather than harder."""
    questions = cards.build_test(deck(3), rng=random.Random(3))
    assert all(q.kind == "written" for q in questions)


def test_the_test_is_no_longer_than_asked_for():
    assert len(cards.build_test(deck(50), length=7, rng=random.Random(4))) == 7


def test_an_empty_deck_is_refused():
    with pytest.raises(cards.CardError, match="no cards"):
        cards.build_test([])


# ------------------------------------------------------------ written grading


@pytest.mark.parametrize(
    "given, expected, verdict",
    [
        ("mitochondrion", "Mitochondrion", "correct"),
        ("the mitochondrion", "mitochondrion", "correct"),   # articles are noise
        ("Mitochondrion.", "mitochondrion", "correct"),      # so is punctuation
        ("the size of each step", "The size of each step", "correct"),
        ("size of the step", "the size of each step", "close"),
        ("a completely different thing", "the size of each step", "wrong"),
        ("", "anything", "wrong"),
    ],
)
def test_written_answers_are_graded_forgivingly_but_honestly(given, expected, verdict):
    assert cards.grade_written(given, expected) == verdict
