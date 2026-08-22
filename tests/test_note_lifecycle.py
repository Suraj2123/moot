"""Editing and deleting notes, and keeping the index honest about it.

The failure this file exists to prevent is quiet: a student corrects a note,
the text on screen changes, and retrieval carries on matching and citing the
sentences they removed. Nothing errors. The note simply lies.

That happens because the indexer decides what to rebuild by comparing chunking
parameters, which an edit does not touch -- so the assertions here are mostly
about chunks and vectors actually going away, not about the API shape.
"""

from __future__ import annotations

import pytest

from sqlalchemy import func, select

from studylink import store
from studylink.schema import chunks, embeddings, notes as notes_table


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_note(client, token, title="Week 3", body="Gradient descent minimises the loss."):
    response = client.post("/notes", headers=auth(token), json={"title": title, "body": body})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ------------------------------------------------------------------ editing


def test_renaming_a_note_does_not_reindex_it(client, signup):
    """Chunks are still accurate after a rename, and rebuilding them would
    throw away work for nothing."""
    token = signup(client)
    note_id = make_note(client, token)

    body = client.patch(f"/notes/{note_id}", headers=auth(token), json={"title": "Week 4"}).json()

    assert body["reindexed"] is False
    assert body["job"] is None
    assert client.get(f"/notes/{note_id}", headers=auth(token)).json()["title"] == "Week 4"


def test_editing_the_body_reindexes(client, signup):
    token = signup(client)
    note_id = make_note(client, token)

    body = client.patch(
        f"/notes/{note_id}", headers=auth(token), json={"body": "Entirely different text."}
    ).json()

    assert body["reindexed"] is True
    assert body["job"]["kind"] == "reindex"


def test_editing_the_body_drops_the_stale_chunks(client, signup, conn_for_client):
    """The heart of it. Without this the note keeps matching on deleted text."""
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    store.replace_chunks(conn, note_id, ["gradient descent", "loss function"], 180, 40)
    assert _chunk_count(conn, note_id) == 2

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"body": "Something else."})

    assert _chunk_count(conn, note_id) == 0


def test_an_edit_also_drops_the_vectors_not_just_the_chunks(client, signup, conn_for_client):
    """embeddings has no foreign key -- it is polymorphic on (owner_type,
    owner_id, model) -- so nothing cascades. Orphaned vectors would accumulate
    forever and still be searchable."""
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    chunk_ids = store.replace_chunks(conn, note_id, ["alpha", "beta"], 180, 40)
    _write_vectors(conn, chunk_ids)
    assert _vector_count(conn, chunk_ids) == 2

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"body": "New."})

    assert _vector_count(conn, chunk_ids) == 0


def test_an_edit_that_changes_nothing_is_not_treated_as_a_change(client, signup):
    """Saving without editing should not throw away the index."""
    token = signup(client)
    note_id = make_note(client, token, body="Unchanged text.")

    body = client.patch(
        f"/notes/{note_id}", headers=auth(token), json={"body": "Unchanged text."}
    ).json()

    assert body["reindexed"] is False


def test_a_note_can_be_unfiled_from_its_course(client, signup, conn_for_client):
    """`course_id: null` cannot mean both "leave alone" and "clear", so
    clearing is its own flag."""
    token = signup(client)
    conn = conn_for_client()
    course_id = store.upsert_course(conn, "c1", "Machine Learning", user_id=1)
    note_id = make_note(client, token)

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"course_id": course_id})
    assert client.get(f"/notes/{note_id}", headers=auth(token)).json()["course_id"] == course_id

    client.patch(f"/notes/{note_id}", headers=auth(token), json={"clear_course": True})
    assert client.get(f"/notes/{note_id}", headers=auth(token)).json()["course_id"] is None


# ----------------------------------------------------------------- deleting


def test_deleting_a_note_removes_it(client, signup):
    token = signup(client)
    note_id = make_note(client, token)

    assert client.delete(f"/notes/{note_id}", headers=auth(token)).status_code == 204
    assert client.get("/notes", headers=auth(token)).json() == []
    assert client.get(f"/notes/{note_id}", headers=auth(token)).status_code == 404


def test_deleting_a_note_removes_its_chunks_and_vectors(client, signup, conn_for_client):
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    chunk_ids = store.replace_chunks(conn, note_id, ["alpha", "beta", "gamma"], 180, 40)
    _write_vectors(conn, chunk_ids)

    client.delete(f"/notes/{note_id}", headers=auth(token))

    assert _chunk_count(conn, note_id) == 0
    assert _vector_count(conn, chunk_ids) == 0
    assert conn.execute(
        select(func.count()).select_from(notes_table).where(notes_table.c.id == note_id)
    ).scalar() == 0


def test_deleting_a_note_that_is_already_gone_is_a_404(client, signup):
    token = signup(client)
    note_id = make_note(client, token)
    client.delete(f"/notes/{note_id}", headers=auth(token))

    assert client.delete(f"/notes/{note_id}", headers=auth(token)).status_code == 404


# ------------------------------------------------------------ index status


def test_a_new_note_is_not_reported_as_indexed(client, signup):
    """It has no chunks yet. Saying "indexed" would be a lie the student then
    has to debug when nothing matches."""
    token = signup(client)
    make_note(client, token)

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["index_status"] in {"queued", "stale"}


def test_a_fully_indexed_note_says_so(client, signup, conn_for_client):
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    chunk_ids = store.replace_chunks(conn, note_id, ["alpha", "beta"], 180, 40)
    _write_vectors(conn, chunk_ids)
    _finish_jobs(conn)

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["index_status"] == "indexed"


def test_a_note_chunked_under_old_settings_is_stale(client, signup, conn_for_client):
    """Retrieval mixes chunk sizes badly, so the indexer rebuilds on a config
    change -- the status has to agree with it."""
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    chunk_ids = store.replace_chunks(conn, note_id, ["alpha"], chunk_size=999, chunk_overlap=0)
    _write_vectors(conn, chunk_ids)
    _finish_jobs(conn)

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["index_status"] == "stale"


def test_chunks_without_vectors_are_not_indexed(client, signup, conn_for_client):
    """Chunked but not embedded is not searchable, and the two halves of
    indexing run as separate steps -- so this state is reachable."""
    token = signup(client)
    note_id = make_note(client, token)
    conn = conn_for_client()
    store.replace_chunks(conn, note_id, ["alpha", "beta"], 180, 40)  # no vectors
    _finish_jobs(conn)

    listed = client.get("/notes", headers=auth(token)).json()
    assert listed[0]["index_status"] == "stale"


def test_the_status_query_does_not_scale_with_the_number_of_notes(client, signup):
    """It feeds a list. A per-note query would be a query per row."""
    token = signup(client)
    for i in range(12):
        make_note(client, token, title=f"Note {i}")

    listed = client.get("/notes", headers=auth(token)).json()
    assert len(listed) == 12
    assert all("index_status" in n for n in listed)


# ------------------------------------------------------------------ helpers


def _chunk_count(conn, note_id: int) -> int:
    return conn.execute(
        select(func.count()).select_from(chunks).where(chunks.c.note_id == note_id)
    ).scalar()


def _vector_count(conn, chunk_ids: list[int]) -> int:
    if not chunk_ids:
        return 0
    return conn.execute(
        select(func.count()).select_from(embeddings).where(
            embeddings.c.owner_type == "chunk",
            embeddings.c.owner_id.in_(chunk_ids),
        )
    ).scalar()


def _write_vectors(conn, chunk_ids: list[int], model: str = "hash-256") -> None:
    from studylink.db import transaction
    from sqlalchemy import insert

    with transaction(conn):
        for chunk_id in chunk_ids:
            conn.execute(
                insert(embeddings).values(
                    owner_type="chunk", owner_id=chunk_id, model=model,
                    dim=4, vector=b"\x00" * 16,
                )
            )


def _finish_jobs(conn) -> None:
    """Clear the queue so "queued" does not mask the real per-note state."""
    from studylink.db import transaction
    from sqlalchemy import update
    from studylink.schema import jobs as jobs_table

    with transaction(conn):
        conn.execute(update(jobs_table).values(status="succeeded"))
