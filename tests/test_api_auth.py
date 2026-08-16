"""The API's authentication boundary.

Two things get tested here, and the second is the one worth the file:

1. Signup, login, logout, and /auth/me behave.
2. **Every other endpoint refuses an unauthenticated request, and no endpoint
   serves one user's data to another.** The second is asserted by enumerating
   the app's own route table rather than by listing endpoints by hand -- a test
   that lists them is a test that silently stops covering the next one added.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from studylink import api as api_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient against a throwaway database.

    The engine is a module global built on first use, so it has to be reset or
    the first test's database is used by all of them.
    """
    monkeypatch.setenv("STUDYLINK_DB", str(tmp_path / "api.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    api_module._engine = None
    with TestClient(api_module.api) as test_client:
        yield test_client
    api_module._engine = None


def signup(client, email="alice@school.edu", password="correct horse battery"):
    response = client.post(
        "/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ the flow


def test_signup_returns_a_token_and_the_user(client):
    body = client.post(
        "/auth/signup",
        json={"email": "Alice@School.edu", "password": "correct horse battery"},
    ).json()

    assert body["token"]
    assert body["user"]["email"] == "alice@school.edu"


def test_signup_rejects_a_duplicate(client):
    signup(client)
    response = client.post(
        "/auth/signup",
        json={"email": "ALICE@SCHOOL.EDU", "password": "another password here"},
    )
    assert response.status_code == 400


def test_signup_rejects_a_weak_password(client):
    response = client.post(
        "/auth/signup", json={"email": "alice@school.edu", "password": "short"}
    )
    assert response.status_code == 400


def test_login_then_me(client):
    signup(client)
    response = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    )
    assert response.status_code == 200
    token = response.json()["token"]

    me = client.get("/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["email"] == "alice@school.edu"
    assert me.json()["auth_source"] == "session"


def test_login_failures_are_indistinguishable(client):
    signup(client)
    responses = [
        client.post("/auth/login", json={"email": "alice@school.edu", "password": "wrong password"}),
        client.post("/auth/login", json={"email": "nobody@school.edu", "password": "correct horse battery"}),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.json()["detail"] for r in responses}) == 1


def test_logout_ends_the_session(client):
    token = signup(client)
    assert client.get("/auth/me", headers=auth(token)).status_code == 200

    assert client.post("/auth/logout", headers=auth(token)).json()["ended"] is True
    assert client.get("/auth/me", headers=auth(token)).status_code == 401


def test_logout_is_always_ok(client):
    """"Log me out" succeeded either way, and reporting the difference would
    tell an unauthorised caller whether the token they hold is live."""
    response = client.post("/auth/logout", headers=auth("not-a-real-token"))
    assert response.status_code == 200
    assert response.json()["ended"] is False


# --------------------------------------------------------------- the boundary


PUBLIC_PATHS = {"/auth/signup", "/auth/login", "/auth/logout",
                "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def protected_routes():
    """Every route that is supposed to require authentication.

    Read off the app itself, not hand-listed, so an endpoint added tomorrow is
    covered tomorrow rather than whenever someone remembers to update a list.
    """
    found = []
    for route in api_module.api.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        if not path or path in PUBLIC_PATHS:
            continue
        for method in methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            found.append((method, path))
    return sorted(found)


def test_there_are_protected_routes_to_check():
    """Guards the guard: if the enumeration breaks, the sweep below would pass
    vacuously by checking nothing at all."""
    assert len(protected_routes()) >= 10


@pytest.mark.parametrize("method,path", protected_routes())
def test_every_endpoint_refuses_an_anonymous_request(client, method, path):
    concrete = path.replace("{assignment_id}", "1").replace("{note_id}", "1")
    response = client.request(method, concrete, json={})
    assert response.status_code == 401, (
        f"{method} {concrete} answered {response.status_code} without a token"
    )


@pytest.mark.parametrize("method,path", protected_routes())
def test_every_endpoint_refuses_a_garbage_token(client, method, path):
    concrete = path.replace("{assignment_id}", "1").replace("{note_id}", "1")
    response = client.request(method, concrete, json={}, headers=auth("nonsense"))
    assert response.status_code == 401


def test_a_revoked_token_stops_working_everywhere(client):
    token = signup(client)
    client.post("/auth/logout", headers=auth(token))

    for method, path in protected_routes():
        concrete = path.replace("{assignment_id}", "1").replace("{note_id}", "1")
        response = client.request(method, concrete, json={}, headers=auth(token))
        assert response.status_code == 401, f"{method} {concrete} still accepted it"


# ------------------------------------------------------------------ isolation


def test_one_users_notes_are_invisible_to_another(client):
    """The whole point of the day. Same topic on both sides, so a scoping bug
    surfaces as the other user's note rather than as an empty list."""
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    client.post(
        "/notes",
        headers=auth(alice),
        json={"title": "Alice's notes", "body": "Gradient descent, ALICE-SECRET-TOKEN"},
    )
    client.post(
        "/notes",
        headers=auth(bob),
        json={"title": "Bob's notes", "body": "Gradient descent, BOB-SECRET-TOKEN"},
    )

    alice_notes = client.get("/notes", headers=auth(alice)).json()
    bob_notes = client.get("/notes", headers=auth(bob)).json()

    assert [n["title"] for n in alice_notes] == ["Alice's notes"]
    assert [n["title"] for n in bob_notes] == ["Bob's notes"]

    alice_search = client.get("/search", params={"q": "gradient descent"}, headers=auth(alice))
    assert "BOB-SECRET-TOKEN" not in alice_search.text


def test_fetching_another_users_note_is_a_404_not_a_403(client):
    """A 403 confirms the row exists and belongs to somebody else, which is the
    fact worth hiding."""
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    created = client.post(
        "/notes",
        headers=auth(alice),
        json={"title": "Alice's notes", "body": "Gradient descent, ALICE-SECRET-TOKEN"},
    ).json()

    response = client.get(f"/notes/{created['id']}/assignments", headers=auth(bob))
    assert response.status_code == 404
    assert "ALICE-SECRET-TOKEN" not in response.text
