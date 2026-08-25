"""Matching notes against something you named yourself.

This is the feature the product is built around, and until now it was
unreachable. `upsert_assignment` had no caller outside the Canvas syncer,
`assignments.course_id` was NOT NULL, and a course only ever came from a sync
-- so the retriever, the evidence layer, the calibrated confidence and the
whole evaluation harness behind them did nothing at all for a student whose
university disables personal access tokens. Which is most of them.

The tests below are mostly about that reachability: a target exists without
Canvas, it matches immediately rather than after a background job, and a synced
assignment and a typed one stay distinguishable so a sync cannot overwrite
someone's own work.
"""

from __future__ import annotations

import pytest


def auth(token):
    return {"Authorization": f"Bearer {token}"}


GRADIENT = (
    "Gradient descent minimises a loss function by stepping downhill. The "
    "learning rate alpha controls the size of each step, and too large an "
    "alpha overshoots the minimum and diverges."
)
ROME = (
    "Augustus became the first Roman emperor in 27 BC after the civil wars "
    "that followed Caesar's assassination ended the republic."
)


def note(client, token, title, body):
    return client.post(
        "/notes", json={"title": title, "body": body}, headers=auth(token)
    ).json()


def target(client, token, name, description=""):
    return client.post(
        "/targets", json={"name": name, "description": description}, headers=auth(token)
    )


# ------------------------------------------------- reachable without Canvas


def test_a_target_can_be_made_with_no_canvas_connection(client, signup):
    """The headline. No token, no course, no sync."""
    token = signup(client)
    response = target(client, token, "Midterm 2: optimisation")

    assert response.status_code == 201
    assert response.json()["name"] == "Midterm 2: optimisation"
    assert response.json()["source"] == "manual"
    assert client.get("/canvas", headers=auth(token)).json()["connected"] is False


def test_a_target_matches_notes_in_the_same_request(client, signup):
    """Typing a topic and seeing which notes cover it is the whole point. A
    version that matches thirty seconds later reads as one that does not work."""
    token = signup(client)
    note(client, token, "Optimisation", GRADIENT)
    note(client, token, "Rome", ROME)

    matches = target(client, token, "learning rate and step size").json()["matches"]
    assert matches, "a note about exactly this should match immediately"
    assert matches[0]["title"] == "Optimisation"


def test_matching_a_target_needs_no_api_key(client, signup, monkeypatch):
    """Free-path purity: this runs on the offline embedding provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    token = signup(client)
    note(client, token, "Optimisation", GRADIENT)
    assert target(client, token, "gradient descent").json()["matches"]


def test_a_target_appears_in_the_list_despite_having_no_course(client, signup):
    """The join to `courses` has to be an outer one. An inner join drops
    exactly the course-less rows, silently, from every list."""
    token = signup(client)
    target(client, token, "Midterm 2")

    listed = client.get("/assignments", headers=auth(token)).json()
    assert [t["name"] for t in listed] == ["Midterm 2"]
    assert listed[0]["course"] in (None, "")


def test_the_description_is_matched_on_not_just_the_name(client, signup):
    """A syllabus section pasted into the description is the richest signal
    anyone will give this feature."""
    token = signup(client)
    note(client, token, "Optimisation", GRADIENT)
    note(client, token, "Rome", ROME)

    matches = target(
        client, token, "Week 4",
        description="Stepping downhill, the learning rate, and divergence.",
    ).json()["matches"]
    assert matches[0]["title"] == "Optimisation"


def test_an_unrelated_target_does_not_claim_a_match(client, signup):
    token = signup(client)
    note(client, token, "Rome", ROME)
    matches = target(client, token, "photosynthesis and chlorophyll").json()["matches"]
    assert matches == []


def test_a_target_with_no_notes_yet_is_not_an_error(client, signup):
    token = signup(client)
    response = target(client, token, "Midterm 2")
    assert response.status_code == 201
    assert response.json()["matches"] == []


def test_an_unnamed_target_gets_a_name(client, signup):
    token = signup(client)
    assert target(client, token, "   ").json()["name"] == "Untitled target"


# ------------------------------------------------------------ editing them


def test_editing_a_target_re_matches_it(client, signup):
    """The text is what it matches on, so changing the text has to change the
    vector -- otherwise the target keeps answering for what it used to say."""
    token = signup(client)
    note(client, token, "Optimisation", GRADIENT)
    note(client, token, "Rome", ROME)
    made = target(client, token, "Roman republic").json()
    assert made["matches"][0]["title"] == "Rome"

    edited = client.patch(
        f"/targets/{made['id']}",
        json={"name": "learning rate and gradient descent"},
        headers=auth(token),
    ).json()
    assert edited["matches"][0]["title"] == "Optimisation"


def test_a_target_can_be_deleted(client, signup):
    token = signup(client)
    made = target(client, token, "Midterm 2").json()

    assert client.delete(f"/targets/{made['id']}", headers=auth(token)).status_code == 204
    assert client.get("/assignments", headers=auth(token)).json() == []


def test_a_due_date_can_be_set_and_cleared(client, signup):
    token = signup(client)
    made = target(client, token, "Midterm 2").json()

    dated = client.patch(
        f"/targets/{made['id']}", json={"due_at": "2026-12-01"}, headers=auth(token)
    ).json()
    assert dated["due_at"] == "2026-12-01"

    cleared = client.patch(
        f"/targets/{made['id']}", json={"clear_due": True}, headers=auth(token)
    ).json()
    assert cleared["due_at"] is None


# ------------------------------------------- synced and typed stay separate


def synced_assignment(client, token, conn_for_client, name="Problem set 3", canvas_id="99"):
    """A row shaped the way the Canvas syncer writes one, without a sync."""
    from studylink import store

    conn = conn_for_client()
    user_id = client.get("/auth/me", headers=auth(token)).json()["id"]
    course_id = store.upsert_course(conn, "1", "Machine Learning", user_id=user_id)
    return store.upsert_assignment(
        conn, course_id=course_id, canvas_id=canvas_id, name=name,
        description="Implement gradient descent.", user_id=user_id,
    )


def test_a_synced_assignment_cannot_be_edited_here(client, signup, conn_for_client):
    """It is a copy of something owned elsewhere. A local edit would be
    overwritten by the next sync, which is the same trap as editing a card that
    a note writes."""
    token = signup(client)
    assignment_id = synced_assignment(client, token, conn_for_client)

    response = client.patch(
        f"/targets/{assignment_id}", json={"name": "mine now"}, headers=auth(token)
    )
    assert response.status_code == 409
    assert "Canvas" in response.json()["detail"]


def test_a_synced_assignment_cannot_be_deleted_here(client, signup, conn_for_client):
    token = signup(client)
    assignment_id = synced_assignment(client, token, conn_for_client)

    response = client.delete(f"/targets/{assignment_id}", headers=auth(token))
    assert response.status_code == 409
    assert "sync" in response.json()["detail"]


def test_synced_and_typed_targets_list_together_and_stay_labelled(
    client, signup, conn_for_client
):
    token = signup(client)
    synced_assignment(client, token, conn_for_client)
    target(client, token, "Midterm 2")

    listed = client.get("/assignments", headers=auth(token)).json()
    by_source = {t["source"]: t["name"] for t in listed}
    assert by_source == {"canvas": "Problem set 3", "manual": "Midterm 2"}


def test_a_sync_does_not_disturb_a_typed_target(client, signup, conn_for_client):
    """The reason `source` exists. A sync replaces what it synced; anything a
    student wrote is none of its business."""
    token = signup(client)
    made = target(client, token, "Midterm 2").json()
    synced_assignment(client, token, conn_for_client)

    still = client.get("/assignments", headers=auth(token)).json()
    assert made["name"] in [t["name"] for t in still]
    assert len(still) == 2


# ---------------------------------------------------------------- ownership


def test_targets_are_scoped_to_their_owner(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    made = target(client, alice, "ALICE-TARGET").json()

    assert client.get("/assignments", headers=auth(bob)).json() == []
    assert client.patch(
        f"/targets/{made['id']}", json={"name": "hacked"}, headers=auth(bob)
    ).status_code == 404
    assert client.delete(f"/targets/{made['id']}", headers=auth(bob)).status_code == 404
    assert client.get(
        f"/assignments/{made['id']}/matches", headers=auth(bob)
    ).status_code == 404


def test_a_target_never_matches_another_users_notes(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")
    note(client, alice, "Optimisation", GRADIENT)

    matches = target(client, bob, "learning rate and step size").json()["matches"]
    assert matches == []


def test_creating_a_target_needs_a_session(client):
    assert client.post("/targets", json={"name": "x"}).status_code == 401
    assert client.patch("/targets/1", json={"name": "x"}).status_code == 401
    assert client.delete("/targets/1").status_code == 401
