"""Writing a set by typing it, the way Quizlet works.

Until this existed there were exactly two ways to get a card: write `::` inside
a note, or pay a model to read one. Neither covers the most ordinary thing a
student wants to do -- open a blank set, type twenty terms and definitions
before an exam, and study them. `store.create_deck` and `store.add_cards`
already worked; nothing routed to them.

The interesting case here is not adding cards. It is what happens when someone
tries to edit a card that a *note* is writing, because accepting that edit and
silently reverting it on the next note save is the kind of bug a student cannot
diagnose.
"""

from __future__ import annotations

import pytest


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def empty_deck(client, token, title="Spanish 101"):
    return client.post("/decks", json={"title": title}, headers=auth(token)).json()


def add(client, token, deck_id, *pairs):
    return client.post(
        f"/decks/{deck_id}/cards",
        json={"cards": [{"front": f, "back": b} for f, b in pairs]},
        headers=auth(token),
    )


# ----------------------------------------------------------- making a deck


def test_a_deck_can_be_made_with_nothing_but_a_title(client, signup):
    token = signup(client)
    deck = client.post("/decks", json={"title": "Spanish 101"}, headers=auth(token))

    assert deck.status_code == 201
    assert deck.json()["title"] == "Spanish 101"
    assert deck.json()["cards"] == 0
    # And it is a real deck: shareable, listable, with a slug of its own.
    assert deck.json()["slug"]
    assert [d["title"] for d in client.get("/decks", headers=auth(token)).json()] == ["Spanish 101"]


def test_making_a_deck_by_hand_needs_no_api_key(client, signup, monkeypatch):
    """Free-path purity: this route must never reach a model."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    token = signup(client)
    deck = empty_deck(client, token)
    assert add(client, token, deck["id"], ("hola", "hello")).status_code == 201


def test_an_untitled_deck_gets_a_name_rather_than_an_error(client, signup):
    token = signup(client)
    assert client.post("/decks", json={}, headers=auth(token)).json()["title"] == "Untitled deck"


def test_a_deck_can_be_renamed(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    renamed = client.patch(
        f"/decks/{deck['id']}", json={"title": "Spanish 102"}, headers=auth(token)
    )
    assert renamed.json()["title"] == "Spanish 102"


def test_renaming_and_publishing_in_one_patch(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    add(client, token, deck["id"], ("hola", "hello"))

    updated = client.patch(
        f"/decks/{deck['id']}",
        json={"title": "Spanish 102", "visibility": "public"},
        headers=auth(token),
    ).json()
    assert updated["title"] == "Spanish 102"
    assert updated["visibility"] == "public"


def test_an_empty_patch_returns_the_deck_rather_than_complaining(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    assert client.patch(f"/decks/{deck['id']}", json={}, headers=auth(token)).json()["id"] == deck["id"]


# -------------------------------------------------------------- adding cards


def test_cards_are_added_in_a_batch(client, signup):
    """People write a set a row at a time and then save, so the endpoint takes
    the whole grid rather than one card per request."""
    token = signup(client)
    deck = empty_deck(client, token)
    response = add(
        client, token, deck["id"],
        ("hola", "hello"), ("adiós", "goodbye"), ("gracias", "thank you"),
    )

    assert response.status_code == 201
    assert [c["front"] for c in response.json()] == ["hola", "adiós", "gracias"]
    assert client.get("/decks", headers=auth(token)).json()[0]["cards"] == 3


def test_new_cards_are_due_immediately(client, signup):
    """An unstudied card is exactly the one that should come up first."""
    token = signup(client)
    deck = empty_deck(client, token)
    add(client, token, deck["id"], ("hola", "hello"))

    assert client.get("/decks", headers=auth(token)).json()[0]["due"] == 1


def test_half_filled_rows_are_skipped_not_refused(client, signup):
    """A row with only a term is what an editor looks like mid-typing."""
    token = signup(client)
    deck = empty_deck(client, token)
    response = add(
        client, token, deck["id"],
        ("hola", "hello"), ("adiós", ""), ("", "nothing"), ("  ", "  "),
    )

    assert response.status_code == 201
    assert [c["front"] for c in response.json()] == ["hola"]


def test_whitespace_around_a_card_is_trimmed(client, signup):
    """Pasted rows arrive with trailing spaces, and " hola" is not a new card."""
    token = signup(client)
    deck = empty_deck(client, token)
    card = add(client, token, deck["id"], ("  hola  ", "\thello\t")).json()[0]
    assert (card["front"], card["back"]) == ("hola", "hello")


def test_cards_can_be_added_to_a_deck_the_model_wrote(client, signup):
    """The three sources are not walled off from each other."""
    token = signup(client)
    deck = empty_deck(client, token)
    add(client, token, deck["id"], ("hola", "hello"))
    add(client, token, deck["id"], ("adiós", "goodbye"))
    assert client.get("/decks", headers=auth(token)).json()[0]["cards"] == 2


# ------------------------------------------------------------ editing cards


def test_a_typed_card_can_be_fixed(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    card = add(client, token, deck["id"], ("hola", "helo")).json()[0]

    fixed = client.patch(f"/cards/{card['id']}", json={"back": "hello"}, headers=auth(token))
    assert fixed.status_code == 200
    assert fixed.json()["back"] == "hello"
    assert fixed.json()["front"] == "hola", "an unnamed field is unchanged"


def test_fixing_a_typo_does_not_reset_the_schedule(client, signup):
    """A typo is not evidence the student has forgotten the card."""
    token = signup(client)
    deck = empty_deck(client, token)
    card = add(client, token, deck["id"], ("hola", "helo")).json()[0]
    client.post(f"/cards/{card['id']}/review", json={"grade": 3}, headers=auth(token))

    before = client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"][0]
    client.patch(f"/cards/{card['id']}", json={"back": "hello"}, headers=auth(token))
    after = client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"][0]

    assert after["back"] == "hello"
    assert (after["reviews"], after["interval_days"], after["due_at"]) == (
        before["reviews"], before["interval_days"], before["due_at"]
    )


def test_a_card_can_be_deleted(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    cards = add(client, token, deck["id"], ("hola", "hello"), ("adiós", "goodbye")).json()

    assert client.delete(f"/cards/{cards[0]['id']}", headers=auth(token)).status_code == 204
    remaining = client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"]
    assert [c["front"] for c in remaining] == ["adiós"]


# ------------------------------------------- the case that would lose an edit


def note_card(client, token):
    """A card declared by `::` inside a note, rather than typed."""
    client.post(
        "/notes",
        json={"title": "Spanish", "body": "hola :: helo"},
        headers=auth(token),
    )
    deck = client.get("/decks", headers=auth(token)).json()[0]
    return deck, client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"][0]


def test_editing_a_card_a_note_writes_is_refused_with_the_reason(client, signup):
    """The next save of that note re-derives the card, so accepting the edit
    here would revert it later with nothing to show the student why."""
    token = signup(client)
    _, card = note_card(client, token)

    response = client.patch(
        f"/cards/{card['id']}", json={"back": "hello"}, headers=auth(token)
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "written by your note" in detail
    assert "Spanish" in detail, "it should say which note to edit"


def test_deleting_a_card_a_note_writes_is_refused_too(client, signup):
    token = signup(client)
    _, card = note_card(client, token)

    response = client.delete(f"/cards/{card['id']}", headers=auth(token))
    assert response.status_code == 409
    assert "written by your note" in response.json()["detail"]


def test_the_note_is_still_the_way_to_change_it(client, signup):
    """Refusing is only defensible because the real route works."""
    token = signup(client)
    client.post("/notes", json={"title": "Spanish", "body": "hola :: helo"}, headers=auth(token))
    note_id = client.get("/notes", headers=auth(token)).json()[0]["id"]

    client.patch(f"/notes/{note_id}", json={"body": "hola :: hello"}, headers=auth(token))
    deck = client.get("/decks", headers=auth(token)).json()[0]
    items = client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"]
    assert items[0]["back"] == "hello"


def test_a_card_typed_into_a_notes_deck_survives_the_note_being_saved(client, signup):
    """A hand-written card has no source_key, so the sync's "cards I own" set
    never contains it and re-deriving the note cannot delete it."""
    token = signup(client)
    client.post("/notes", json={"title": "Spanish", "body": "hola :: hello"}, headers=auth(token))
    note_id = client.get("/notes", headers=auth(token)).json()[0]["id"]
    deck = client.get("/decks", headers=auth(token)).json()[0]

    add(client, token, deck["id"], ("por favor", "please"))
    client.patch(
        f"/notes/{note_id}",
        json={"body": "hola :: hello\nadiós :: goodbye"},
        headers=auth(token),
    )

    fronts = {
        c["front"]
        for c in client.get(f"/decks/{deck['id']}", headers=auth(token)).json()["items"]
    }
    assert fronts == {"hola", "adiós", "por favor"}


# ---------------------------------------------------------------- ownership


def test_another_user_cannot_touch_your_cards(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    deck = empty_deck(client, alice)
    card = add(client, alice, deck["id"], ("hola", "hello")).json()[0]

    assert add(client, bob, deck["id"], ("x", "y")).status_code == 404
    assert client.patch(
        f"/cards/{card['id']}", json={"back": "hacked"}, headers=auth(bob)
    ).status_code == 404
    assert client.delete(f"/cards/{card['id']}", headers=auth(bob)).status_code == 404
    assert client.patch(
        f"/decks/{deck['id']}", json={"title": "hacked"}, headers=auth(bob)
    ).status_code == 404

    still = client.get(f"/decks/{deck['id']}", headers=auth(alice)).json()
    assert still["title"] == "Spanish 101"
    assert still["items"][0]["back"] == "hello"


def test_writing_cards_needs_a_session(client, signup):
    token = signup(client)
    deck = empty_deck(client, token)
    assert client.post(f"/decks/{deck['id']}/cards", json={"cards": []}).status_code == 401
    assert client.patch("/cards/1", json={"front": "x"}).status_code == 401
    assert client.delete("/cards/1").status_code == 401


def test_a_published_deck_stays_findable_after_cards_are_typed_in(client, signup):
    """Search text is the cards, so adding them has to re-index."""
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    deck = empty_deck(client, owner, title="Cells")
    add(client, owner, deck["id"], ("mitochondrion", "the powerhouse of the cell"))
    client.patch(f"/decks/{deck['id']}", json={"visibility": "public"}, headers=auth(owner))

    add(client, owner, deck["id"], ("chloroplast", "where photosynthesis happens"))
    found = client.get("/discover?q=photosynthesis", headers=auth(seeker)).json()
    assert [d["title"] for d in found] == ["Cells"]
