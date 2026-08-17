"""Connecting Canvas per account.

The point of these: a token goes in and never comes back out, and one account's
Canvas is never reachable from another's session.
"""

from __future__ import annotations

import pytest

from studylink import vault


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def connect_body(url="https://canvas.school.edu", token="canvas-secret-token"):
    return {"api_url": url, "api_token": token}


def test_canvas_starts_disconnected(client, signup):
    token = signup(client)
    assert client.get("/canvas", headers=auth(token)).json() == {"connected": False}


def test_connect_then_status(client, signup):
    token = signup(client)

    response = client.post("/canvas/connect", headers=auth(token), json=connect_body())
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["api_url"] == "https://canvas.school.edu"

    status = client.get("/canvas", headers=auth(token)).json()
    assert status["connected"] is True
    assert status["verified_at"] is None


def test_the_token_never_comes_back(client, signup):
    """There is no read path for a stored token outside the syncer."""
    token = signup(client)
    secret = "canvas-secret-token"

    connect = client.post("/canvas/connect", headers=auth(token),
                          json=connect_body(token=secret))
    status = client.get("/canvas", headers=auth(token))

    assert secret not in connect.text
    assert secret not in status.text


def test_disconnect_forgets_it(client, signup):
    token = signup(client)
    client.post("/canvas/connect", headers=auth(token), json=connect_body())

    response = client.delete("/canvas", headers=auth(token))
    assert response.json() == {"connected": False, "removed": True}
    assert client.get("/canvas", headers=auth(token)).json() == {"connected": False}


def test_one_account_cannot_see_anothers_canvas(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    client.post("/canvas/connect", headers=auth(alice),
                json=connect_body("https://alice-canvas.edu", "alice-secret"))

    bob_status = client.get("/canvas", headers=auth(bob))
    assert bob_status.json() == {"connected": False}
    assert "alice-secret" not in bob_status.text
    assert "alice-canvas" not in bob_status.text


def test_disconnecting_does_not_touch_another_account(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    client.post("/canvas/connect", headers=auth(alice), json=connect_body())
    client.post("/canvas/connect", headers=auth(bob), json=connect_body())

    client.delete("/canvas", headers=auth(alice))

    assert client.get("/canvas", headers=auth(bob)).json()["connected"] is True


def test_bad_urls_are_rejected(client, signup):
    token = signup(client)
    for bad in ["", "canvas.school.edu", "ftp://canvas.edu"]:
        response = client.post("/canvas/connect", headers=auth(token),
                               json=connect_body(url=bad))
        assert response.status_code == 400, bad


def test_syncing_without_canvas_connected_is_a_clear_error(client, signup):
    """Rather than silently syncing against whatever is in the process
    environment, which is the bug this whole commit exists to close."""
    token = signup(client)
    response = client.post("/sync", headers=auth(token))

    assert response.status_code == 400
    assert "not connected" in response.json()["detail"].lower()


def test_sync_does_not_fall_back_to_the_environment(client, signup, monkeypatch):
    """A web request must never pick up the deployment's own Canvas token."""
    monkeypatch.setenv("CANVAS_API_URL", "https://someone-elses-canvas.edu")
    monkeypatch.setenv("CANVAS_API_TOKEN", "deployment-token")

    token = signup(client)
    response = client.post("/sync", headers=auth(token))

    assert response.status_code == 400
    assert "someone-elses-canvas" not in response.text
    assert "deployment-token" not in response.text


def test_connecting_without_a_server_key_is_refused(client, signup, monkeypatch):
    monkeypatch.delenv(vault.ENV_VAR, raising=False)
    token = signup(client)

    response = client.post("/canvas/connect", headers=auth(token), json=connect_body())
    assert response.status_code == 400
    assert "STUDYLINK_SECRET_KEY" in response.json()["detail"]
