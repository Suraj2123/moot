"""Background work through the API.

Two things worth asserting: a write returns before the work is done, and a job
belongs to exactly one account.
"""

from __future__ import annotations

import pytest

from studylink import jobs, vault
from studylink.worker import run_once


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_creating_a_note_queues_the_reindex(client, signup):
    """The note is durable on return; it becomes searchable when the job runs."""
    token = signup(client)

    response = client.post(
        "/notes",
        headers=auth(token),
        json={"title": "Gradient descent", "body": "Alpha controls the step size."},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["job"]["kind"] == jobs.KIND_REINDEX
    assert body["job"]["status"] == jobs.QUEUED


def test_reindex_is_accepted_not_performed(client, signup):
    token = signup(client)
    response = client.post("/reindex", headers=auth(token))

    assert response.status_code == 202
    assert response.json()["status"] == jobs.QUEUED


def test_sync_without_canvas_still_fails_fast(client, signup):
    """A job that fails 30 seconds later is a worse way to learn this."""
    token = signup(client)
    response = client.post("/sync", headers=auth(token))

    assert response.status_code == 400
    assert "not connected" in response.json()["detail"].lower()


def test_sync_is_queued_once_canvas_is_connected(client, signup):
    token = signup(client)
    client.post(
        "/canvas/connect",
        headers=auth(token),
        json={"api_url": "https://canvas.school.edu", "api_token": "secret"},
    )

    response = client.post("/sync", headers=auth(token))
    assert response.status_code == 202
    assert response.json()["kind"] == jobs.KIND_CANVAS_SYNC


def test_repeated_requests_coalesce(client, signup):
    token = signup(client)
    first = client.post("/reindex", headers=auth(token)).json()
    second = client.post("/reindex", headers=auth(token)).json()

    assert first["id"] == second["id"]
    assert len(client.get("/jobs", headers=auth(token)).json()) == 1


def test_job_status_is_readable(client, signup):
    token = signup(client)
    job = client.post("/reindex", headers=auth(token)).json()

    fetched = client.get(f"/jobs/{job['id']}", headers=auth(token))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == job["id"]


def test_one_account_cannot_read_anothers_job(client, signup):
    alice = signup(client, "alice@school.edu")
    bob = signup(client, "bob@school.edu")

    job = client.post("/reindex", headers=auth(alice)).json()

    assert client.get(f"/jobs/{job['id']}", headers=auth(bob)).status_code == 404
    assert client.get("/jobs", headers=auth(bob)).json() == []


def test_a_note_becomes_searchable_after_the_worker_runs(client, signup):
    """End to end: queue it, run the worker, and the note is retrievable."""
    import studylink.api as api_module

    token = signup(client)
    client.post(
        "/notes",
        headers=auth(token),
        json={"title": "Gradient descent",
              "body": "Gradient descent steps downhill; alpha sets the step size."},
    )

    found = client.get("/search", params={"q": "gradient descent"}, headers=auth(token))
    assert found.json() == [], "indexing should not have happened inline"

    with api_module.engine().connect() as conn:
        finished = run_once(conn)
    assert finished.status == jobs.SUCCEEDED, finished.detail

    found = client.get("/search", params={"q": "gradient descent"}, headers=auth(token))
    assert len(found.json()) >= 1


def test_a_failing_job_is_recorded_not_swallowed(client, signup):
    """Canvas will reject the fake token; the job must record that clearly."""
    import studylink.api as api_module

    token = signup(client)
    client.post(
        "/canvas/connect",
        headers=auth(token),
        json={"api_url": "https://canvas.invalid", "api_token": "bad-token"},
    )
    job = client.post("/sync", headers=auth(token)).json()

    with api_module.engine().connect() as conn:
        finished = run_once(conn)

    assert finished.status == jobs.FAILED
    assert finished.detail
    assert "bad-token" not in finished.detail

    reported = client.get(f"/jobs/{job['id']}", headers=auth(token)).json()
    assert reported["status"] == jobs.FAILED
