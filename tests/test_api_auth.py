"""The API's authentication boundary.

Two things get tested here, and the second is the one worth the file:

1. Signup, login, logout, and /auth/me behave.
2. **Every other endpoint refuses an unauthenticated request, and no endpoint
   serves one user's data to another.** The second is asserted by enumerating
   the app's own route table rather than by listing endpoints by hand -- a test
   that lists them is a test that silently stops covering the next one added.
"""

from __future__ import annotations

import re

import pytest
from studylink import api as api_module


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


def test_signup_rejects_a_duplicate(client, signup):
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


def test_login_then_me(client, signup):
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


def test_login_failures_are_indistinguishable(client, signup):
    signup(client)
    responses = [
        client.post("/auth/login", json={"email": "alice@school.edu", "password": "wrong password"}),
        client.post("/auth/login", json={"email": "nobody@school.edu", "password": "correct horse battery"}),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.json()["detail"] for r in responses}) == 1


def test_logout_ends_the_session(client, signup):
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


# Public on purpose. The first three are how you get a session in the first
# place; the docs routes are FastAPI's own. `/` and `/app/...` serve the built
# frontend, which has to load before anyone can sign in -- it is static files
# with no user data in them, and every call the app then makes is authenticated
# individually.
PUBLIC_PATHS = {"/auth/signup", "/auth/login", "/auth/logout",
                "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect",
                "/", "/app/{path:path}"}


def concrete(path: str) -> str:
    """Fill any path parameter with 1, so this keeps working for new routes.

    Naming the parameters individually is the same trap as hand-listing the
    routes: it works until someone adds one.
    """
    return re.sub(r"\{[^}]+\}", "1", path)


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


def test_serving_the_frontend_exposes_no_user_data(client, signup):
    """`/app/...` is public, so it must be static files and nothing else.

    A path traversal here would turn the one unauthenticated route into a file
    reader, so the traversal attempt is asserted rather than assumed.
    """
    token = signup(client)
    client.post("/notes", headers=auth(token),
                json={"title": "Private", "body": "ALICE-SECRET-TOKEN"})

    for path in ["/app/", "/app/index.html", "/app/../../studylink/api.py",
                 "/app/%2e%2e/%2e%2e/etc/passwd"]:
        response = client.get(path)
        assert "ALICE-SECRET-TOKEN" not in response.text, path
        assert "STUDYLINK_SECRET_KEY" not in response.text, path
        assert "root:" not in response.text, path


def test_there_are_protected_routes_to_check():
    """Guards the guard: if the enumeration breaks, the sweep below would pass
    vacuously by checking nothing at all."""
    assert len(protected_routes()) >= 10


@pytest.mark.parametrize("method,path", protected_routes())
def test_every_endpoint_refuses_an_anonymous_request(client, method, path):
    response = client.request(method, concrete(path), json={})
    assert response.status_code == 401, (
        f"{method} {concrete(path)} answered {response.status_code} without a token"
    )


@pytest.mark.parametrize("method,path", protected_routes())
def test_every_endpoint_refuses_a_garbage_token(client, method, path):
    response = client.request(method, concrete(path), json={}, headers=auth("nonsense"))
    assert response.status_code == 401


def test_a_revoked_token_stops_working_everywhere(client, signup):
    token = signup(client)
    client.post("/auth/logout", headers=auth(token))

    for method, path in protected_routes():
        target = concrete(path)
        response = client.request(method, target, json={}, headers=auth(token))
        assert response.status_code == 401, f"{method} {target} still accepted it"


# ------------------------------------------------------------------ isolation


def test_one_users_notes_are_invisible_to_another(client, signup):
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


def test_fetching_another_users_note_is_a_404_not_a_403(client, signup):
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


# ----------------------------------------------------------- session management


def test_listing_sessions_shows_only_your_own(client, signup):
    alice = signup(client, "alice@school.edu")
    signup(client, "bob@school.edu")
    client.post("/auth/login", json={"email": "alice@school.edu", "password": "correct horse battery"})

    listed = client.get("/auth/sessions", headers=auth(alice)).json()
    assert len(listed) == 2  # signup issued one, login another
    assert all("token" not in str(k).lower() for row in listed for k in row)


def test_session_listing_never_returns_token_hashes(client, signup):
    """Useless to the owner, and one stray log line from being a problem."""
    token = signup(client)
    body = client.get("/auth/sessions", headers=auth(token)).text
    assert "token_hash" not in body
    assert token not in body


def test_revoking_your_own_session_ends_it(client, signup):
    token = signup(client)
    other = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    ).json()["token"]

    # Newest first, so [-1] is the session signup issued -- revoke that one while
    # authenticated as the newer session, or the request kills its own caller.
    listed = client.get("/auth/sessions", headers=auth(other)).json()
    target = listed[-1]

    assert client.delete(f"/auth/sessions/{target['id']}", headers=auth(other)).status_code == 200

    remaining = client.get("/auth/sessions", headers=auth(other)).json()
    assert target["id"] not in [s["id"] for s in remaining]
    assert client.get("/auth/me", headers=auth(token)).status_code == 401


def test_you_cannot_revoke_someone_elses_session(client, signup):
    """The ownership predicate is in the WHERE clause; this is what proves it."""
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    alice_session = client.get("/auth/sessions", headers=auth(alice)).json()[0]

    response = client.delete(f"/auth/sessions/{alice_session['id']}", headers=auth(bob))
    assert response.status_code == 404

    # And Alice's session still works.
    assert client.get("/auth/me", headers=auth(alice)).status_code == 200


def test_revoke_others_keeps_the_calling_session(client, signup):
    """Logging yourself out as a side effect of securing your account is a small
    thing that stops people pressing the button."""
    first = signup(client)
    second = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    ).json()["token"]

    response = client.post("/auth/sessions/revoke-others", headers=auth(second))
    assert response.status_code == 200
    assert response.json()["ended"] == 1

    assert client.get("/auth/me", headers=auth(second)).status_code == 200
    assert client.get("/auth/me", headers=auth(first)).status_code == 401


def test_revoke_others_does_not_touch_another_account(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    client.post("/auth/sessions/revoke-others", headers=auth(alice))

    assert client.get("/auth/me", headers=auth(bob)).status_code == 200


# ------------------------------------------------------------ password change


def test_changing_the_password_works_and_the_old_one_stops(client, signup):
    token = signup(client)

    response = client.post(
        "/auth/password",
        headers=auth(token),
        json={"current_password": "correct horse battery",
              "new_password": "a different long password"},
    )
    assert response.status_code == 200

    old = client.post("/auth/login",
                      json={"email": "alice@school.edu", "password": "correct horse battery"})
    assert old.status_code == 401

    new = client.post("/auth/login",
                      json={"email": "alice@school.edu", "password": "a different long password"})
    assert new.status_code == 200


def test_changing_the_password_ends_every_other_session(client, signup):
    """The point of the feature, and the usual way it is built wrong.

    Changing a password while the attacker's session keeps working does not
    remove the attacker.
    """
    stolen = signup(client)
    mine = client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    ).json()["token"]

    response = client.post(
        "/auth/password",
        headers=auth(mine),
        json={"current_password": "correct horse battery",
              "new_password": "a different long password"},
    )
    assert response.json()["sessions_ended"] == 1

    assert client.get("/auth/me", headers=auth(stolen)).status_code == 401
    assert client.get("/auth/me", headers=auth(mine)).status_code == 200


def test_changing_the_password_requires_the_current_one(client, signup):
    """A stolen token alone must not be enough to take the account permanently
    -- which is the situation this feature exists to recover from."""
    token = signup(client)

    response = client.post(
        "/auth/password",
        headers=auth(token),
        json={"current_password": "not the right password",
              "new_password": "a different long password"},
    )
    assert response.status_code == 401

    # And the password is unchanged.
    assert client.post(
        "/auth/login",
        json={"email": "alice@school.edu", "password": "correct horse battery"},
    ).status_code == 200


def test_the_new_password_must_be_valid_and_different(client, signup):
    token = signup(client)

    short = client.post(
        "/auth/password",
        headers=auth(token),
        json={"current_password": "correct horse battery", "new_password": "short"},
    )
    assert short.status_code == 400

    same = client.post(
        "/auth/password",
        headers=auth(token),
        json={"current_password": "correct horse battery",
              "new_password": "correct horse battery"},
    )
    assert same.status_code == 400


def test_changing_a_password_does_not_touch_another_account(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    client.post(
        "/auth/password",
        headers=auth(alice),
        json={"current_password": "correct horse battery",
              "new_password": "a different long password"},
    )

    assert client.get("/auth/me", headers=auth(bob)).status_code == 200
    assert client.post(
        "/auth/login",
        json={"email": "bob@school.edu", "password": "correct horse battery"},
    ).status_code == 200
