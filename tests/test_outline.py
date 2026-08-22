"""Notes written as an outline, with cards declared inline.

No model is involved, so these are all exact-value assertions. The ones worth
reading are about identity across edits: a student who fixes a typo in an
answer must keep the card's review history, and one who rewrites a question has
made a different card.
"""

from __future__ import annotations

import pytest

from studylink import outline


def fronts(text: str) -> list[str]:
    return [card["front"] for card in outline.to_cards(text)]


def backs(text: str) -> list[str]:
    return [card["back"] for card in outline.to_cards(text)]


# ------------------------------------------------------------------- syntax


def test_a_double_colon_line_is_a_card():
    cards = outline.to_cards("learning rate :: controls the step size")
    assert cards == [{
        "front": "learning rate",
        "back": "controls the step size",
        "evidence": "learning rate :: controls the step size",
        "kind": "pair",
        "source_key": cards[0]["source_key"],
    }]


def test_an_arrow_is_the_same_as_a_double_colon():
    assert fronts("momentum >> accumulates a velocity vector") == ["momentum"]


def test_a_triple_colon_makes_both_directions():
    """Recognising a term and producing it are separate skills, so they are
    separate cards with separate schedules."""
    text = "mitochondrion ::: powerhouse of the cell"
    assert fronts(text) == ["mitochondrion", "powerhouse of the cell"]
    assert backs(text) == ["powerhouse of the cell", "mitochondrion"]


def test_the_triple_colon_is_checked_before_the_double():
    """`::` is a prefix of `:::`, so matching it first would turn every
    bidirectional card into a forward one with a stray colon on the answer."""
    assert backs("a ::: b") == ["b", "a"]
    assert ":" not in backs("a ::: b")[0]


def test_a_line_with_no_separator_is_just_a_note():
    assert outline.to_cards("Photosynthesis happens in the chloroplast") == []


def test_a_separator_with_nothing_on_one_side_is_not_a_card():
    assert outline.to_cards("incomplete ::") == []
    assert outline.to_cards(":: orphan") == []


def test_bullets_are_optional_and_stripped():
    assert fronts("- learning rate :: step size") == ["learning rate"]
    assert fronts("* learning rate :: step size") == ["learning rate"]


def test_blank_lines_are_ignored():
    assert len(outline.to_cards("a :: b\n\n\nc :: d")) == 2


# -------------------------------------------------------------------- cloze


def test_a_cloze_hides_its_own_span():
    cards = outline.to_cards("The capital of France is {{Paris}}")
    assert cards[0]["front"] == "The capital of France is ____"
    assert cards[0]["back"] == "Paris"
    assert cards[0]["kind"] == "cloze"


def test_each_cloze_on_a_line_is_its_own_card():
    """One card testing four facts is the thing spaced repetition is
    specifically bad at."""
    cards = outline.to_cards("{{Mitosis}} produces {{two}} identical cells")
    assert len(cards) == 2
    assert [c["back"] for c in cards] == ["Mitosis", "two"]


def test_other_deletions_stay_visible_as_context():
    cards = outline.to_cards("{{Mitosis}} produces {{two}} identical cells")
    assert cards[0]["front"] == "____ produces two identical cells"
    assert cards[1]["front"] == "Mitosis produces ____ identical cells"


def test_cloze_evidence_is_the_line_without_the_braces():
    card = outline.to_cards("The capital of France is {{Paris}}")[0]
    assert card["evidence"] == "The capital of France is Paris"


def test_an_empty_cloze_is_not_a_card():
    assert outline.to_cards("nothing here {{}}") == []


# ------------------------------------------------------------------ nesting


def test_a_nested_card_inherits_its_parent_as_context():
    """A bare "light reactions" could belong to any lecture in the semester."""
    text = "Photosynthesis\n  light reactions :: split water, release oxygen"
    assert fronts(text) == ["Photosynthesis › light reactions"]


def test_context_accumulates_through_several_levels():
    text = "Biology\n  Photosynthesis\n    light reactions :: split water"
    assert fronts(text) == ["Biology › Photosynthesis › light reactions"]


def test_dedenting_drops_the_branch_that_ended():
    text = (
        "Photosynthesis\n"
        "  light reactions :: split water\n"
        "Respiration\n"
        "  glycolysis :: splits glucose\n"
    )
    assert fronts(text) == [
        "Photosynthesis › light reactions",
        "Respiration › glycolysis",
    ]


def test_a_card_can_itself_be_a_parent():
    """Notes nest under the concept they elaborate, and that concept is often
    the card."""
    text = "cell ::: basic unit of life\n  nucleus :: holds the DNA"
    assert "cell › nucleus" in fronts(text)


def test_a_tab_counts_as_one_level():
    text = "Photosynthesis\n\tlight reactions :: split water"
    assert fronts(text) == ["Photosynthesis › light reactions"]


def test_four_spaces_read_as_two_levels():
    text = "A\n  B\n    c :: d"
    assert fronts(text) == ["A › B › c"]


def test_a_cloze_is_not_given_ancestry():
    """The sentence already carries its context; prefixing asks the student to
    read the same fact twice."""
    text = "Biology\n  The capital of France is {{Paris}}"
    assert fronts(text) == ["The capital of France is ____"]


# ----------------------------------------------------------------- identity


def test_editing_the_answer_keeps_the_cards_identity():
    """Three weeks of review history must survive a typo fix."""
    before = outline.to_cards("learning rate :: contorls step size")[0]
    after = outline.to_cards("learning rate :: controls the step size")[0]
    assert before["source_key"] == after["source_key"]


def test_rewriting_the_question_makes_a_new_card():
    a = outline.to_cards("learning rate :: step size")[0]
    b = outline.to_cards("what is alpha :: step size")[0]
    assert a["source_key"] != b["source_key"]


def test_identity_survives_reformatting():
    a = outline.to_cards("learning rate :: step size")[0]
    b = outline.to_cards("-   Learning   Rate   ::   step size")[0]
    assert a["source_key"] == b["source_key"]


def test_the_two_directions_have_different_identities():
    cards = outline.to_cards("a ::: b")
    assert cards[0]["source_key"] != cards[1]["source_key"]


def test_moving_a_card_under_a_different_parent_changes_its_identity():
    """Its question genuinely changed -- the context is part of what is asked."""
    a = outline.to_cards("Biology\n  cell :: unit of life")[0]
    b = outline.to_cards("Chemistry\n  cell :: unit of life")[0]
    assert a["source_key"] != b["source_key"]


# -------------------------------------------------------------------- misc


def test_counting_is_what_the_editor_shows():
    text = "a :: b\nc ::: d\ne is {{f}}"
    assert outline.count(text) == 4  # 1 + 2 + 1


def test_an_empty_note_declares_nothing():
    assert outline.to_cards("") == []
    assert outline.count("   \n\n  ") == 0


def test_an_absurdly_long_side_is_not_treated_as_a_card():
    """Prose containing a colon is not a flashcard, and turning a paragraph
    into one produces something unanswerable."""
    text = "Note: " + ("x" * (outline.MAX_SIDE_CHARS + 10))
    assert outline.to_cards(text) == []


@pytest.mark.parametrize("text", [
    "a :: b :: c",
    "{{a}} :: b",
    "-- :: --",
])
def test_ambiguous_lines_do_not_raise(text):
    outline.to_cards(text)  # a parser that throws on odd input is unusable
