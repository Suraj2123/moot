"""Public decks: slugs, discovery, and forking.

`test_isolation.py` covers the question "can the wrong person see this". This
file covers the rest of sharing -- that a link resolves, that search finds what
it should and ranks it sensibly, that a fork is a real independent copy, and
that the whole path works with no API key, because discovery runs on the same
offline embedding provider as everything else on the free path.
"""

from __future__ import annotations

import pytest

from studylink import slugs, store


def auth(token):
    return {"Authorization": f"Bearer {token}"}


BIOLOGY = "mitochondrion :: the powerhouse of the cell\nribosome :: builds proteins"
HISTORY = "Augustus :: the first Roman emperor\nthe Rubicon :: the river Caesar crossed"


def deck_from(client, token, title, body):
    client.post("/notes", json={"title": title, "body": body}, headers=auth(token))
    decks = client.get("/decks", headers=auth(token)).json()
    return next(d for d in decks if d["title"] == title)


def publish(client, token, deck, visibility="public"):
    return client.patch(
        f"/decks/{deck['id']}", json={"visibility": visibility}, headers=auth(token)
    ).json()


# ------------------------------------------------------------------- slugs


def test_a_slug_avoids_characters_that_get_misread():
    """Share links get read aloud and typed back in. `0`/`O` and `1`/`l` are
    where that goes wrong, and vowels are how a random string spells something
    the owner did not intend to send to a class group chat."""
    alphabet = set(slugs.ALPHABET)
    assert not (alphabet & set("01lIO"))
    assert not (alphabet & set("aeiouAEIOU"))


def test_slugs_do_not_repeat_across_many_decks():
    seen = {slugs.new_slug() for _ in range(2000)}
    assert len(seen) == 2000


def test_a_deck_has_its_slug_before_it_is_ever_shared(client, signup):
    """Sharing is a switch someone expects to hand them a link immediately."""
    token = signup(client)
    deck = deck_from(client, token, "Biology", BIOLOGY)
    assert deck["slug"]
    assert deck["visibility"] == "private"


# ------------------------------------------------------------- the link page


def test_a_shared_deck_names_its_owner_by_display_name(client):
    client.post(
        "/auth/signup",
        json={
            "email": "owner@school.edu",
            "password": "correct horse battery",
            "display_name": "Priya",
        },
    )
    token = client.post(
        "/auth/login", json={"email": "owner@school.edu", "password": "correct horse battery"}
    ).json()["token"]
    deck = deck_from(client, token, "Biology", BIOLOGY)
    publish(client, token, deck)

    assert client.get(f"/d/{deck['slug']}").json()["owner"] == "Priya"


def test_an_owner_with_no_display_name_is_not_identified(client, signup):
    token = signup(client, email="anon@school.edu")
    deck = deck_from(client, token, "Biology", BIOLOGY)
    publish(client, token, deck)

    body = client.get(f"/d/{deck['slug']}").json()
    assert body["owner"] == "A moot user"
    assert "anon@school.edu" not in str(body)


def test_a_browser_navigating_to_a_share_link_gets_the_app(client, signup, monkeypatch):
    """One URL for a human and for code. A browser sends `text/html` on a
    navigation and gets the page; the page then fetches the same URL as JSON."""
    from studylink import api as api_module

    index = api_module.STATIC_DIR / "index.html"
    if not index.exists():
        pytest.skip("the SPA has not been built")

    token = signup(client)
    deck = deck_from(client, token, "Biology", BIOLOGY)
    publish(client, token, deck)

    html = client.get(
        f"/d/{deck['slug']}", headers={"Accept": "text/html,application/xhtml+xml"}
    )
    assert html.status_code == 200
    assert "<!doctype html" in html.text.lower()

    data = client.get(f"/d/{deck['slug']}", headers={"Accept": "application/json"})
    assert data.json()["title"] == "Biology"


def test_an_unknown_slug_is_a_404_not_a_crash(client, signup):
    token = signup(client)
    assert client.get("/d/nosuchdeck", headers={"Accept": "application/json"}).status_code == 404
    assert client.post("/d/nosuchdeck/fork", headers=auth(token)).status_code == 404


def test_visibility_only_accepts_the_three_it_documents(client, signup):
    token = signup(client)
    deck = deck_from(client, token, "Biology", BIOLOGY)
    response = client.patch(
        f"/decks/{deck['id']}", json={"visibility": "everyone"}, headers=auth(token)
    )
    assert response.status_code == 422
    assert "private" in response.json()["detail"]


# --------------------------------------------------------------- discovery


def test_discovery_ranks_the_deck_that_matches(client, signup):
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    bio = deck_from(client, owner, "Biology", BIOLOGY)
    rome = deck_from(client, owner, "Rome", HISTORY)
    publish(client, owner, bio)
    publish(client, owner, rome)

    found = client.get("/discover?q=the powerhouse of the cell", headers=auth(seeker)).json()
    assert found, "a public deck about exactly this should be findable"
    assert found[0]["title"] == "Biology"


def test_discovery_answers_a_word_that_only_appears_in_an_answer(client, signup):
    """Half a deck's vocabulary is on the backs of its cards, and a searcher
    does not know which side of a card their words are on."""
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    deck = deck_from(client, owner, "Biology", BIOLOGY)
    publish(client, owner, deck)

    found = client.get("/discover?q=powerhouse", headers=auth(seeker)).json()
    assert [d["title"] for d in found] == ["Biology"]


def test_an_unrelated_search_finds_nothing_rather_than_the_closest_thing(client, signup):
    """A cosine search always returns its top k however badly they match.
    Without a floor the page claims a match it does not have."""
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    publish(client, owner, deck_from(client, owner, "Rome", HISTORY))

    assert client.get("/discover?q=photosynthesis chlorophyll", headers=auth(seeker)).json() == []


def test_discovery_with_no_query_lists_what_exists(client, signup):
    """Someone who opens the page before typing should see decks exist."""
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    publish(client, owner, deck_from(client, owner, "Biology", BIOLOGY))
    publish(client, owner, deck_from(client, owner, "Rome", HISTORY))

    titles = {d["title"] for d in client.get("/discover", headers=auth(seeker)).json()}
    assert titles == {"Biology", "Rome"}


def test_discovery_reports_a_card_count(client, signup):
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    publish(client, owner, deck_from(client, owner, "Biology", BIOLOGY))

    assert client.get("/discover", headers=auth(seeker)).json()[0]["cards"] == 2


def test_editing_a_published_deck_updates_what_search_sees(client, signup):
    """A deck's text changes when its note does, and an index that remembers
    the old text answers for a deck that no longer exists in that shape."""
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    note = client.post(
        "/notes", json={"title": "Biology", "body": BIOLOGY}, headers=auth(owner)
    ).json()
    deck = client.get("/decks", headers=auth(owner)).json()[0]
    publish(client, owner, deck)

    client.patch(
        f"/notes/{note['id']}",
        json={"body": BIOLOGY + "\nchloroplast :: where photosynthesis happens"},
        headers=auth(owner),
    )

    found = client.get("/discover?q=photosynthesis", headers=auth(seeker)).json()
    assert [d["title"] for d in found] == ["Biology"]


def test_discovery_needs_no_api_key(client, signup, monkeypatch):
    """Free-path purity. Search runs on the default offline provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    owner = signup(client, email="owner@school.edu")
    seeker = signup(client, email="seeker@school.edu")
    publish(client, owner, deck_from(client, owner, "Biology", BIOLOGY))

    assert client.get("/discover?q=mitochondrion", headers=auth(seeker)).json()


# ------------------------------------------------------------------ forking


def test_a_fork_is_independent_of_its_original(client, signup):
    """Studying a copy must not touch the deck it came from, and deleting the
    original must not take the copy with it."""
    owner = signup(client, email="owner@school.edu")
    forker = signup(client, email="forker@school.edu")
    deck = deck_from(client, owner, "Biology", BIOLOGY)
    publish(client, owner, deck)

    client.post(f"/d/{deck['slug']}/fork", headers=auth(forker))
    copy = client.get("/decks", headers=auth(forker)).json()[0]

    for card in client.get(f"/decks/{copy['id']}/study", headers=auth(forker)).json():
        client.post(f"/cards/{card['id']}/review", json={"grade": 2}, headers=auth(forker))

    assert client.get("/decks", headers=auth(owner)).json()[0]["due"] == 2

    client.delete(f"/decks/{deck['id']}", headers=auth(owner))
    survivor = client.get("/decks", headers=auth(forker)).json()
    assert len(survivor) == 1 and survivor[0]["cards"] == 2
    # The trail is gone with the original, which is the point of SET NULL.
    assert survivor[0]["forked_from_id"] is None


def test_a_fork_is_not_published_by_inheritance(client, signup):
    """Copying a public deck must not publish the copy. Visibility is a choice
    its new owner has not made yet."""
    owner = signup(client, email="owner@school.edu")
    forker = signup(client, email="forker@school.edu")
    deck = deck_from(client, owner, "Biology", BIOLOGY)
    publish(client, owner, deck)

    forked = client.post(f"/d/{deck['slug']}/fork", headers=auth(forker)).json()
    assert forked["visibility"] == "private"
    assert client.get(f"/d/{forked['slug']}").status_code == 404


def test_an_unlisted_deck_can_still_be_forked_by_link(client, signup):
    """"Anyone with the link" includes copying it -- otherwise the link is
    read-only in a way nobody asked for."""
    owner = signup(client, email="owner@school.edu")
    forker = signup(client, email="forker@school.edu")
    deck = deck_from(client, owner, "Biology", BIOLOGY)
    publish(client, owner, deck, visibility="unlisted")

    assert client.post(f"/d/{deck['slug']}/fork", headers=auth(forker)).status_code == 201


def test_a_fork_reproduces_the_deck_exactly(client, signup):
    """Every card, in order, with the same text. A fork that silently drops or
    reorders cards is worse than one that fails."""
    owner = signup(client, email="owner@school.edu")
    forker = signup(client, email="forker@school.edu")
    body = "\n".join(f"term{n} :: definition{n}" for n in range(12))
    deck = deck_from(client, owner, "Long deck", body)
    publish(client, owner, deck)

    client.post(f"/d/{deck['slug']}/fork", headers=auth(forker))
    copy = client.get("/decks", headers=auth(forker)).json()[0]

    original = client.get(f"/d/{deck['slug']}").json()["cards"]
    copied = client.get(f"/decks/{copy['id']}/study?limit=50", headers=auth(forker)).json()
    assert [(c["front"], c["back"]) for c in copied] == [
        (c["front"], c["back"]) for c in original
    ]


def test_the_store_refuses_an_unknown_visibility(conn, two_users):
    with pytest.raises(ValueError, match="visibility"):
        store.set_deck_visibility(conn, 1, two_users["alice"], "world-readable")
