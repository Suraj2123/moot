"""Somewhere to put notes.

A flat list works until about thirty notes and then stops; one semester
produces more than that. Folders are the smallest thing that fixes it, and the
only decision in them worth defending is what happens when one is deleted.
"""

from __future__ import annotations

import pytest


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def folder(client, token, name):
    return client.post("/folders", json={"name": name}, headers=auth(token))


def note(client, token, title, body="a :: b", folder_id=None):
    return client.post(
        "/notes",
        json={"title": title, "body": body, "folder_id": folder_id},
        headers=auth(token),
    ).json()


# ------------------------------------------------------------- making them


def test_a_folder_can_be_made_and_listed(client, signup):
    token = signup(client)
    made = folder(client, token, "Biology 101")

    assert made.status_code == 201
    assert made.json()["name"] == "Biology 101"
    assert [f["name"] for f in client.get("/folders", headers=auth(token)).json()] == [
        "Biology 101"
    ]


def test_several_folders_coexist(client, signup):
    """"Let them create multiple folders" is the whole feature."""
    token = signup(client)
    for name in ["Biology", "Chemistry", "Roman history"]:
        folder(client, token, name)

    listed = client.get("/folders", headers=auth(token)).json()
    assert [f["name"] for f in listed] == ["Biology", "Chemistry", "Roman history"]


def test_folders_may_share_a_name(client, signup):
    """No uniqueness constraint. Two folders called "Week 1" under different
    subjects is a filing style, not a mistake to refuse."""
    token = signup(client)
    assert folder(client, token, "Week 1").status_code == 201
    assert folder(client, token, "Week 1").status_code == 201
    assert len(client.get("/folders", headers=auth(token)).json()) == 2


def test_an_unnamed_folder_gets_a_name(client, signup):
    token = signup(client)
    assert folder(client, token, "  ").json()["name"] == "Untitled folder"


def test_a_folder_can_be_renamed(client, signup):
    token = signup(client)
    made = folder(client, token, "Bio").json()

    renamed = client.patch(
        f"/folders/{made['id']}", json={"name": "Biology 101"}, headers=auth(token)
    )
    assert renamed.json()["name"] == "Biology 101"


# ---------------------------------------------------------- filing notes


def test_a_note_can_be_created_inside_a_folder(client, signup):
    token = signup(client)
    made = folder(client, token, "Biology").json()
    note(client, token, "Cells", folder_id=made["id"])

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["folder_id"] == made["id"]
    assert listed[0]["folder"] == "Biology"


def test_a_note_starts_unfiled_and_that_is_normal(client, signup):
    token = signup(client)
    note(client, token, "Loose thought")

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["folder_id"] is None
    assert listed[0]["folder"] == ""


def test_a_note_can_be_moved_between_folders(client, signup):
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    chem = folder(client, token, "Chemistry").json()
    made = note(client, token, "Cells", folder_id=bio["id"])

    client.post(
        f"/notes/{made['id']}/folder", json={"folder_id": chem["id"]}, headers=auth(token)
    )
    assert client.get("/notes", headers=auth(token)).json()[0]["folder"] == "Chemistry"


def test_a_note_can_be_taken_out_of_every_folder(client, signup):
    """Null is a destination, not a missing value."""
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    made = note(client, token, "Cells", folder_id=bio["id"])

    client.post(f"/notes/{made['id']}/folder", json={"folder_id": None}, headers=auth(token))
    assert client.get("/notes", headers=auth(token)).json()[0]["folder_id"] is None


def test_the_note_list_can_be_filtered_to_one_folder(client, signup):
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    note(client, token, "Cells", folder_id=bio["id"])
    note(client, token, "Loose thought")

    filed = client.get(f"/notes?folder_id={bio['id']}", headers=auth(token)).json()
    assert [n["title"] for n in filed] == ["Cells"]

    unfiled = client.get("/notes?unfiled=true", headers=auth(token)).json()
    assert [n["title"] for n in unfiled] == ["Loose thought"]


def test_a_folder_reports_how_many_notes_it_holds(client, signup):
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    note(client, token, "Cells", folder_id=bio["id"])
    note(client, token, "Enzymes", folder_id=bio["id"])
    note(client, token, "Elsewhere")

    assert client.get("/folders", headers=auth(token)).json()[0]["notes"] == 2


# ------------------------------------------------- the one that matters


def test_deleting_a_folder_keeps_the_notes(client, signup):
    """The decision worth defending. Clicking "delete folder" is a filing
    decision, and there is no reading of that click on which the student meant
    to destroy a semester of notes."""
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    note(client, token, "Cells", body="mitochondrion :: powerhouse", folder_id=bio["id"])

    assert client.delete(f"/folders/{bio['id']}", headers=auth(token)).status_code == 204

    remaining = client.get("/notes", headers=auth(token)).json()
    assert [n["title"] for n in remaining] == ["Cells"]
    assert remaining[0]["folder_id"] is None


def test_deleting_a_folder_keeps_the_cards_its_notes_declared(client, signup):
    token = signup(client)
    bio = folder(client, token, "Biology").json()
    note(client, token, "Cells", body="mitochondrion :: powerhouse", folder_id=bio["id"])
    client.delete(f"/folders/{bio['id']}", headers=auth(token))

    decks = client.get("/decks", headers=auth(token)).json()
    assert decks and decks[0]["cards"] == 1


# ---------------------------------------------------------------- ownership


def test_folders_are_scoped_to_their_owner(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    mine = folder(client, alice, "ALICE-FOLDER").json()

    assert client.get("/folders", headers=auth(bob)).json() == []
    assert client.patch(
        f"/folders/{mine['id']}", json={"name": "hacked"}, headers=auth(bob)
    ).status_code == 404
    assert client.delete(f"/folders/{mine['id']}", headers=auth(bob)).status_code == 404

    assert client.get("/folders", headers=auth(alice)).json()[0]["name"] == "ALICE-FOLDER"


def test_a_note_cannot_be_filed_into_someone_elses_folder(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    hers = folder(client, alice, "Biology").json()

    created = client.post(
        "/notes",
        json={"title": "Mine", "body": "x", "folder_id": hers["id"]},
        headers=auth(bob),
    )
    assert created.status_code == 404

    his = note(client, bob, "Mine")
    moved = client.post(
        f"/notes/{his['id']}/folder", json={"folder_id": hers["id"]}, headers=auth(bob)
    )
    assert moved.status_code == 404


def test_folder_endpoints_need_a_session(client):
    assert client.get("/folders").status_code == 401
    assert client.post("/folders", json={"name": "x"}).status_code == 401
    assert client.delete("/folders/1").status_code == 401
