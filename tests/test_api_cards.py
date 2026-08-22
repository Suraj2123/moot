"""Decks and cards through the API.

`test_cards.py` covers generation, scheduling, and test assembly. What is left
here is what only the endpoints can get wrong: metering the model call, scoping
decks to their owner, and the study loop actually moving a card's due date.
"""

from __future__ import annotations

import json

import pytest

from studylink import cards as cards_module
from studylink import service as service_module


NOTE_BODY = (
    "Gradient descent minimises a loss function by stepping downhill. "
    "The learning rate alpha controls the size of each step. "
    "Too large an alpha overshoots the minimum and diverges."
)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class StubWriter:
    """Stands in for CardWriter so no test needs a key or a network."""

    model = "stub-model"

    def __init__(self, cards=None, rejected=0, usage=None):
        self.cards = cards if cards is not None else [
            cards_module.DraftCard("What does alpha control?", "Step size", NOTE_BODY[:40]),
            cards_module.DraftCard("What happens if alpha is too large?", "It diverges", NOTE_BODY[:40]),
            cards_module.DraftCard("What does gradient descent minimise?", "A loss function", NOTE_BODY[:40]),
            cards_module.DraftCard("Which direction does it step?", "Downhill", NOTE_BODY[:40]),
        ]
        self.rejected = rejected
        self.usage = usage if usage is not None else {"input_tokens": 500, "output_tokens": 300}

    def write(self, title, body, count=10):
        return cards_module.Generated(
            cards=list(self.cards)[:count], rejected=self.rejected, usage=self.usage
        )


@pytest.fixture
def stub_writer(monkeypatch):
    """Patch the writer the service constructs, not the module it lives in."""
    writer = StubWriter()
    monkeypatch.setattr(cards_module, "CardWriter", lambda *a, **k: writer)
    return writer


def make_note(client, token, body=NOTE_BODY):
    return client.post(
        "/notes", headers=auth(token), json={"title": "Lecture 4", "body": body}
    ).json()["id"]


def make_deck(client, token, note_id, **kwargs):
    return client.post(
        "/decks", headers=auth(token), json={"note_id": note_id, **kwargs}
    )


# ------------------------------------------------------------------ creation


def test_a_deck_is_generated_from_a_note(client, signup, stub_writer):
    token = signup(client)
    note_id = make_note(client, token)

    response = make_deck(client, token, note_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["cards"] == 4
    assert body["rejected"] == 0

    decks = client.get("/decks", headers=auth(token)).json()
    assert decks[0]["title"] == "Lecture 4"
    assert decks[0]["cards"] == 4
    assert decks[0]["due"] == 4, "new cards are due immediately"


def test_the_number_of_ungrounded_cards_is_reported(client, signup, monkeypatch):
    """Not a promise that generation is grounded -- a measurement of how often
    it was not."""
    monkeypatch.setattr(
        cards_module, "CardWriter", lambda *a, **k: StubWriter(rejected=3)
    )
    token = signup(client)
    response = make_deck(client, token, make_note(client, token))
    assert response.json()["rejected"] == 3


def test_a_note_that_yields_no_grounded_cards_is_a_stated_error(client, signup, monkeypatch):
    monkeypatch.setattr(
        cards_module, "CardWriter", lambda *a, **k: StubWriter(cards=[], rejected=5)
    )
    token = signup(client)
    response = make_deck(client, token, make_note(client, token))
    assert response.status_code == 422
    assert "grounded" in response.json()["detail"]


def test_generation_is_metered(client, signup, stub_writer):
    """Invariant: every model call goes through the ledger. Cards are a new
    caller and would otherwise spend silently."""
    token = signup(client)
    before = client.get("/usage", headers=auth(token)).json()["calls"]

    make_deck(client, token, make_note(client, token))

    after = client.get("/usage", headers=auth(token)).json()
    assert after["calls"] == before + 1
    assert after["input_tokens"] == 500


def test_no_model_configured_is_a_503_not_a_500(client, signup, monkeypatch):
    def unavailable(*a, **k):
        raise cards_module.GenerationUnavailable("No Anthropic credentials found.")

    monkeypatch.setattr(cards_module, "CardWriter", unavailable)
    token = signup(client)
    response = make_deck(client, token, make_note(client, token))
    assert response.status_code == 503


def test_a_short_note_is_refused_with_a_reason(client, signup):
    token = signup(client)
    note_id = make_note(client, token, body="Too short.")
    response = make_deck(client, token, note_id)
    assert response.status_code == 422
    assert "too short" in response.json()["detail"]


def test_a_custom_title_is_used(client, signup, stub_writer):
    token = signup(client)
    make_deck(client, token, make_note(client, token), title="Midterm 1")
    assert client.get("/decks", headers=auth(token)).json()[0]["title"] == "Midterm 1"


# -------------------------------------------------------------------- study


def test_cards_carry_the_sentence_they_came_from(client, signup, stub_writer):
    """The grounding, visible in the product rather than only enforced in the
    pipeline -- a student can check any card against their own note."""
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()

    assert all(card["evidence"] for card in deck["items"])
    assert all(card["note_id"] for card in deck["items"])


def test_reviewing_a_card_schedules_it_forward(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]

    result = client.post(
        f"/cards/{card_id}/review", headers=auth(token), json={"grade": 2}
    ).json()

    assert result["interval_days"] == 1
    remaining = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()
    assert card_id not in [c["id"] for c in remaining], "a reviewed card leaves the queue"


def test_forgetting_keeps_the_card_coming_back(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]

    client.post(f"/cards/{card_id}/review", headers=auth(token), json={"grade": 0})

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    card = next(c for c in deck["items"] if c["id"] == card_id)
    assert card["lapses"] == 1
    assert card["reviews"] == 1


def test_an_invalid_grade_is_refused(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]
    assert client.post(
        f"/cards/{card_id}/review", headers=auth(token), json={"grade": 9}
    ).status_code == 422


def test_deck_progress_is_reported(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(token)).json()[0]["id"]
    client.post(f"/cards/{card_id}/review", headers=auth(token), json={"grade": 2})

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    assert deck["cards"] == 4
    assert deck["studied"] == 1
    assert deck["due"] == 3


# ------------------------------------------------------------ practice tests


def test_a_practice_test_is_built_from_the_deck(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]

    body = client.get(f"/decks/{deck_id}/test", headers=auth(token)).json()

    assert len(body["questions"]) == 4
    for question in body["questions"]:
        assert question["answer"] in question["choices"]


def test_a_written_test_offers_no_choices(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    body = client.get(f"/decks/{deck_id}/test?kind=written", headers=auth(token)).json()
    assert all(q["choices"] == [] for q in body["questions"])


def test_an_unknown_test_kind_is_refused(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]
    assert client.get(
        f"/decks/{deck_id}/test?kind=telepathy", headers=auth(token)
    ).status_code == 422


def test_a_typed_answer_is_graded_forgivingly(client, signup):
    token = signup(client)
    response = client.post(
        "/cards/check", headers=auth(token),
        json={"given": "the step size", "expected": "Step size"},
    )
    assert response.json()["verdict"] == "correct"


# ------------------------------------------------------------------ scoping


def test_a_deck_is_not_visible_to_another_account(client, signup, stub_writer):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    deck_id = make_deck(client, alice, make_note(client, alice)).json()["deck_id"]

    assert client.get("/decks", headers=auth(bob)).json() == []
    assert client.get(f"/decks/{deck_id}", headers=auth(bob)).status_code == 404
    assert client.get(f"/decks/{deck_id}/study", headers=auth(bob)).status_code == 404
    assert client.get(f"/decks/{deck_id}/test", headers=auth(bob)).status_code == 404
    assert client.delete(f"/decks/{deck_id}", headers=auth(bob)).status_code == 404


def test_a_card_cannot_be_reviewed_by_another_account(client, signup, stub_writer):
    """Otherwise one account could drive another's schedule, which is quiet
    vandalism rather than a leak -- and just as unwelcome."""
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    deck_id = make_deck(client, alice, make_note(client, alice)).json()["deck_id"]
    card_id = client.get(f"/decks/{deck_id}/study", headers=auth(alice)).json()[0]["id"]

    assert client.post(
        f"/cards/{card_id}/review", headers=auth(bob), json={"grade": 2}
    ).status_code == 404


def test_cards_cannot_be_made_from_another_users_note(client, signup, stub_writer):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    note_id = make_note(client, alice)

    assert make_deck(client, bob, note_id).status_code == 404


def test_deleting_a_deck_takes_its_cards(client, signup, stub_writer):
    token = signup(client)
    deck_id = make_deck(client, token, make_note(client, token)).json()["deck_id"]

    assert client.delete(f"/decks/{deck_id}", headers=auth(token)).status_code == 204
    assert client.get("/decks", headers=auth(token)).json() == []


def test_deleting_the_source_note_keeps_the_deck(client, signup, stub_writer):
    """A fortnight of review history should not evaporate because the note it
    started from was tidied away."""
    token = signup(client)
    note_id = make_note(client, token)
    deck_id = make_deck(client, token, note_id).json()["deck_id"]

    client.delete(f"/notes/{note_id}", headers=auth(token))

    deck = client.get(f"/decks/{deck_id}", headers=auth(token)).json()
    assert deck["cards"] == 4
    assert all(card["note_id"] is None for card in deck["items"]), "traceability is gone"
    assert all(card["front"] for card in deck["items"]), "but the cards survive"
