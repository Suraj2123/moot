"""Cards declared inside a note, kept in step with it as it is edited.

`test_outline.py` covers the parser. What is only observable here is what
happens to a card that already exists when its note changes -- and the answer
has to be "keeps its review history", because that history is the only part of
a flashcard that took weeks to produce.
"""

from __future__ import annotations

import pytest

from sqlalchemy import func, select

from studylink.schema import card_reviews, cards, decks


def auth(token):
    return {"Authorization": f"Bearer {token}"}


NOTE = (
    "Optimisation\n"
    "  learning rate :: controls the step size\n"
    "  momentum :: accumulates a velocity vector\n"
)


def make_note(client, token, body=NOTE, title="Lecture 6"):
    return client.post(
        "/notes", headers=auth(token), json={"title": title, "body": body}
    ).json()["id"]


def deck_of(client, token):
    listed = client.get("/decks", headers=auth(token)).json()
    return listed[0] if listed else None


def card_fronts(client, token, deck_id):
    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    return sorted(card["front"] for card in deck["items"])


# ------------------------------------------------------------------ syncing


def test_saving_a_note_creates_its_deck(client, signup):
    token = signup(client)
    make_note(client, token)

    deck = deck_of(client, token)
    assert deck["title"] == "Lecture 6"
    assert deck["cards"] == 2


def test_cards_carry_their_parent_as_context(client, signup):
    token = signup(client)
    make_note(client, token)
    deck = deck_of(client, token)
    assert "Optimisation › learning rate" in card_fronts(client, token, deck["id"])


def test_a_note_with_no_card_syntax_makes_no_deck(client, signup):
    token = signup(client)
    make_note(client, token, body="Just some prose about gradients.")
    assert client.get("/decks", headers=auth(token)).json() == []


def test_adding_a_line_adds_a_card(client, signup):
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = deck_of(client, token)["id"]

    client.patch(
        f"/notes/{note_id}", headers=auth(token),
        json={"body": NOTE + "  batch norm :: rescales activations\n"},
    )

    assert len(card_fronts(client, token, deck_id)) == 3


def test_removing_a_line_removes_its_card(client, signup):
    """A student who deletes a line is saying they no longer want to be asked
    it. Leaving the card behind would strand it where nothing can edit it."""
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = deck_of(client, token)["id"]

    client.patch(
        f"/notes/{note_id}", headers=auth(token),
        json={"body": "Optimisation\n  learning rate :: controls the step size\n"},
    )

    assert card_fronts(client, token, deck_id) == ["Optimisation › learning rate"]


def test_fixing_a_typo_in_an_answer_keeps_the_review_history(client, signup, conn_for_client):
    """The assertion this whole file exists for. Three weeks of scheduling must
    survive a one-character correction."""
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = deck_of(client, token)["id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]
    client.post(f"/cards/{card_id}/review", headers=auth(token), json={"grade": 2})

    client.patch(
        f"/notes/{note_id}", headers=auth(token),
        json={"body": NOTE.replace("controls the step size", "controls the step size (alpha)")},
    )

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    card = next(c for c in deck["items"] if c["id"] == card_id)
    assert card["reviews"] == 1, "the card is the same card"
    assert card["back"] == "controls the step size (alpha)", "and its answer updated"

    conn = conn_for_client()
    assert conn.execute(
        select(func.count()).select_from(card_reviews).where(card_reviews.c.card_id == card_id)
    ).scalar() == 1


def test_rewriting_a_question_makes_a_new_card(client, signup):
    """A different question is a different card, and starting its schedule
    fresh is correct -- the student has not been tested on this one."""
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = deck_of(client, token)["id"]
    before = client.get(f"/decks/{deck_id}", headers=auth(token)).json()["items"]
    original_ids = {c["id"] for c in before}

    client.patch(
        f"/notes/{note_id}", headers=auth(token),
        json={"body": NOTE.replace("learning rate ::", "what is alpha ::")},
    )

    after = client.get(f"/decks/{deck_id}", headers=auth(token)).json()["items"]
    assert len(after) == 2
    assert {c["id"] for c in after} != original_ids


def test_renaming_the_note_renames_its_deck(client, signup):
    token = signup(client)
    note_id = make_note(client, token)

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"title": "Week 6 recap"})

    assert deck_of(client, token)["title"] == "Week 6 recap"


def test_emptying_a_note_removes_its_deck(client, signup):
    """An empty deck that can never be studied is clutter, not a record."""
    token = signup(client)
    note_id = make_note(client, token)
    client.patch(f"/notes/{note_id}", headers=auth(token), json={"body": "just prose now"})
    assert client.get("/decks", headers=auth(token)).json() == []


def test_duplicate_lines_make_one_card(client, signup):
    """Two identical questions cannot be told apart in review."""
    token = signup(client)
    make_note(client, token, body="a :: b\na :: b\n")
    assert deck_of(client, token)["cards"] == 1


def test_a_bidirectional_line_makes_two_cards(client, signup):
    token = signup(client)
    make_note(client, token, body="mitochondrion ::: powerhouse of the cell\n")
    assert deck_of(client, token)["cards"] == 2


def test_cloze_lines_become_cards(client, signup):
    token = signup(client)
    make_note(client, token, body="The capital of France is {{Paris}}\n")
    deck = deck_of(client, token)
    assert deck["cards"] == 1
    fronts = card_fronts(client, token, deck["id"])
    assert fronts == ["The capital of France is ____"]


def test_a_generated_deck_is_not_touched_by_a_note_edit(client, signup, monkeypatch):
    """The sync owns the note's own deck and nothing else. Deleting somebody's
    generated cards because they edited a line would be unforgivable."""
    from studylink import cards as cards_module
    from tests.test_api_cards import StubWriter

    monkeypatch.setattr(cards_module, "CardWriter", lambda *a, **k: StubWriter())
    token = signup(client)
    note_id = make_note(client, token, body="Some prose. " * 20)
    generated = client.post(
        "/decks", headers=auth(token), json={"note_id": note_id}
    ).json()["deck_id"]

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"body": "a :: b"})

    still = client.get(f"/decks/{generated}", headers=auth(token)).json()
    assert still["cards"] == 4


def test_deleting_a_note_leaves_its_cards_studyable(client, signup):
    """Deliberate, and worth stating: the deck outlives the note it came from.

    The alternative is destroying a fortnight of review history because
    somebody tidied up their notes. The cards stop being synced -- nothing can
    edit them any more, since there is no source text -- but they are still
    the thing the student learned, so they stay.
    """
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = deck_of(client, token)["id"]

    client.delete(f"/notes/{note_id}", headers=auth(token))

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    assert deck["cards"] == 2
    assert all(card["note_id"] is None for card in deck["items"])


# ----------------------------------------------------------------- preview


def test_the_editor_can_preview_without_saving(client, signup):
    token = signup(client)
    body = client.post(
        "/outline/preview", headers=auth(token),
        json={"text": "a :: b\nc ::: d"},
    ).json()
    assert len(body["cards"]) == 3


def test_previewing_stores_nothing(client, signup):
    token = signup(client)
    client.post("/outline/preview", headers=auth(token), json={"text": "a :: b"})
    assert client.get("/decks", headers=auth(token)).json() == []


# ---------------------------------------------------------------- progress


def test_progress_reports_states_and_streak(client, signup):
    token = signup(client)
    make_note(client, token)
    deck_id = deck_of(client, token)["id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]
    client.post(f"/cards/{card_id}/review", headers=auth(token), json={"grade": 2})

    body = client.get("/progress", headers=auth(token)).json()

    assert body["cards"] == 2
    assert body["new"] == 1
    assert body["learning"] == 1
    assert body["streak_days"] == 1
    assert body["accuracy"] == 1.0


def test_a_card_only_ever_answered_correctly_is_not_weak(client, signup):
    """Otherwise everything answered once sits in the practice list until it
    has been right three times running -- which is most of the deck."""
    token = signup(client)
    make_note(client, token)
    deck_id = deck_of(client, token)["id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]
    client.post(f"/cards/{card_id}/review", headers=auth(token), json={"grade": 2})

    assert client.get("/progress/weak", headers=auth(token)).json() == []


def test_weak_cards_surface_what_was_missed(client, signup):
    token = signup(client)
    make_note(client, token)
    deck_id = deck_of(client, token)["id"]
    queue = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()
    missed = queue[0]["id"]
    for _ in range(3):
        client.post(f"/cards/{missed}/review", headers=auth(token), json={"grade": 0})
    client.post(f"/cards/{queue[1]['id']}/review", headers=auth(token), json={"grade": 3})

    weak = client.get("/progress/weak", headers=auth(token)).json()

    assert [c["id"] for c in weak] == [missed]
    assert weak[0]["attempts"] == 3
    assert weak[0]["correct"] == 0


def test_progress_is_scoped_to_the_account(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    make_note(client, alice)

    assert client.get("/progress", headers=auth(bob)).json()["cards"] == 0
    assert client.get("/progress/weak", headers=auth(bob)).json() == []


def test_an_account_with_nothing_gets_a_usable_summary(client, signup):
    token = signup(client)
    body = client.get("/progress", headers=auth(token)).json()
    assert body["cards"] == 0
    assert body["accuracy"] is None
    assert body["streak_days"] == 0
