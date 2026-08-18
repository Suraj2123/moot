"""The chat endpoints.

The model is faked -- what is under test is the boundary: authentication,
per-account isolation, budget enforcement, and whether streaming actually
streams.
"""

from __future__ import annotations

import json

import pytest

from studylink import chat as chat_module
from studylink import usage, vault
from tests.test_chat import FakeClient


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    """Every NoteChat built inside the API gets the fake client."""
    real_init = chat_module.NoteChat.__init__

    def patched(self, conn, retriever, user_id, client=None, **kwargs):
        real_init(self, conn, retriever, user_id,
                  client=client or FakeClient("Gradient descent [N1]."), **kwargs)

    monkeypatch.setattr(chat_module.NoteChat, "__init__", patched)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def add_note(client, token, title="Gradient descent",
             body="Gradient descent steps downhill; alpha sets the step size."):
    client.post("/notes", headers=auth(token), json={"title": title, "body": body})
    # Indexing is a background job now, so run it inline for the test.
    import studylink.api as api_module
    from studylink.worker import run_once

    with api_module.engine().connect() as conn:
        run_once(conn)


def test_ask_requires_authentication(client):
    assert client.post("/ask", json={"question": "anything"}).status_code == 401


def test_ask_answers_from_your_notes(client, signup):
    token = signup(client)
    add_note(client, token)

    response = client.post("/ask", headers=auth(token), json={"question": "gradient descent"})

    assert response.status_code == 200
    body = response.json()
    assert body["text"]
    assert body["grounded"] is True
    assert body["sources"]


def test_ask_refuses_when_nothing_matches(client, signup):
    token = signup(client)
    add_note(client, token)

    body = client.post(
        "/ask", headers=auth(token), json={"question": "melting point of tungsten"}
    ).json()

    assert body["refused"] is True
    assert body["sources"] == []


def test_ask_never_reaches_another_account_s_notes(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")
    add_note(client, alice, body="Gradient descent, ALICE-SECRET-TOKEN.")

    response = client.post(
        "/ask", headers=auth(bob), json={"question": "gradient descent"}
    )

    assert "ALICE-SECRET-TOKEN" not in response.text
    assert response.json()["sources"] == []


def test_an_empty_question_is_a_400(client, signup):
    token = signup(client)
    assert client.post("/ask", headers=auth(token), json={"question": "  "}).status_code == 400


def test_an_exhausted_budget_is_a_429(client, signup, monkeypatch):
    import studylink.api as api_module

    token = signup(client)
    add_note(client, token)

    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.000001")
    with api_module.engine().connect() as conn:
        me = client.get("/auth/me", headers=auth(token)).json()
        usage.record(conn, me["id"], usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    response = client.post("/ask", headers=auth(token), json={"question": "gradient descent"})
    assert response.status_code == 429


def test_usage_is_reported_and_is_per_account(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")
    add_note(client, alice)

    client.post("/ask", headers=auth(alice), json={"question": "gradient descent"})

    assert client.get("/usage", headers=auth(alice)).json()["calls"] == 1
    assert client.get("/usage", headers=auth(bob)).json()["calls"] == 0


# ------------------------------------------------------------------ streaming


def _sse_events(response):
    return [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_streaming_sends_sources_then_text_then_done(client, signup):
    token = signup(client)
    add_note(client, token)

    response = client.post(
        "/ask/stream", headers=auth(token), json={"question": "gradient descent"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response)
    assert events[0]["type"] == "sources"
    assert any(e["type"] == "text" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"]["grounded"] is True


def test_streaming_is_not_buffered_by_a_proxy(client, signup):
    """Nginx buffers proxied responses by default, which holds the whole stream
    until it finishes and turns streaming back into waiting."""
    token = signup(client)
    add_note(client, token)

    response = client.post(
        "/ask/stream", headers=auth(token), json={"question": "gradient descent"}
    )
    assert response.headers.get("x-accel-buffering") == "no"
    assert response.headers.get("cache-control") == "no-store"


def test_streaming_rejects_a_bad_request_before_opening_the_stream(client, signup):
    """Once an SSE stream is open the status is already 200, and an error can
    only be an event a naive client may render as an answer."""
    token = signup(client)

    response = client.post("/ask/stream", headers=auth(token), json={"question": ""})
    assert response.status_code == 400
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_streaming_checks_the_budget_before_opening_the_stream(client, signup, monkeypatch):
    import studylink.api as api_module

    token = signup(client)
    add_note(client, token)

    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.000001")
    with api_module.engine().connect() as conn:
        me = client.get("/auth/me", headers=auth(token)).json()
        usage.record(conn, me["id"], usage.KIND_CHAT, "claude-opus-5", 1_000_000, 1_000_000)

    response = client.post(
        "/ask/stream", headers=auth(token), json={"question": "gradient descent"}
    )
    assert response.status_code == 429


def test_streaming_requires_authentication(client):
    assert client.post("/ask/stream", json={"question": "x"}).status_code == 401
