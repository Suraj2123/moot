"""Uploading a document through the API.

`test_documents.py` covers extraction itself. What is left here is the part
only the endpoint can get wrong: the size cap being enforced while reading
rather than after, an unusable file coming back as a stated 4xx rather than a
500, and an upload being scoped to the account that made it.
"""

from __future__ import annotations

import io

import pytest

from studylink import documents
from tests.test_documents import make_docx, make_pdf


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, token, filename, data, **fields):
    return client.post(
        "/notes/upload",
        headers=auth(token),
        files={"file": (filename, io.BytesIO(data), "application/octet-stream")},
        data=fields,
    )


# ---------------------------------------------------------------- the path


def test_a_markdown_upload_becomes_a_note(client, signup):
    token = signup(client)
    body = b"# Week 3\n\nGradient descent minimises the loss."

    response = upload(client, token, "week3.md", body)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["chars"] == len(body.decode())

    notes = client.get("/notes", headers=auth(token)).json()
    assert [n["title"] for n in notes] == ["week3"]


def test_a_pdf_upload_becomes_a_note(client, signup):
    token = signup(client)
    data = make_pdf(["Bayes rule relates conditional probabilities."])

    response = upload(client, token, "lecture 4.pdf", data)

    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "PDF"
    assert [n["title"] for n in client.get("/notes", headers=auth(token)).json()] == [
        "lecture 4"
    ]


def test_a_word_upload_becomes_a_note(client, signup):
    token = signup(client)
    response = upload(client, token, "handout.docx", make_docx(["Entropy measures uncertainty."]))
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "Word"


def test_an_explicit_title_wins_over_the_filename(client, signup):
    token = signup(client)
    upload(client, token, "untitled-3.md", b"content here", title="Bayes, week 4")
    assert [n["title"] for n in client.get("/notes", headers=auth(token)).json()] == [
        "Bayes, week 4"
    ]


def test_an_upload_queues_indexing_like_a_typed_note_does(client, signup):
    """Otherwise the file saves and is never searchable -- the same failure
    the worker exists to prevent, reintroduced on a new path."""
    token = signup(client)
    response = upload(client, token, "notes.md", b"some content")
    assert response.json()["job"]["kind"] == "reindex"


def test_a_truncation_notice_reaches_the_caller(client, signup, monkeypatch):
    token = signup(client)
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 1)
    response = upload(client, token, "textbook.pdf", make_pdf(["one", "two"]))
    assert "1 of 2" in response.json()["notice"]


# ------------------------------------------------------------------ refusals


def test_an_unsupported_type_is_refused_with_the_list(client, signup):
    token = signup(client)
    response = upload(client, token, "lecture.mp4", b"\x00\x00\x00 ftypmp42")
    assert response.status_code == 415
    assert ".pdf" in response.json()["detail"]


def test_an_oversized_upload_is_refused(client, signup):
    token = signup(client)
    response = upload(client, token, "huge.txt", b"x" * (documents.MAX_BYTES + 1024))
    assert response.status_code == 413


def test_the_size_cap_is_checked_while_reading_not_after(client, signup, monkeypatch):
    """A cap applied after reading has already let the whole file into memory,
    which is the thing the cap is for."""
    monkeypatch.setattr(documents, "MAX_BYTES", 4096)
    seen = {"max": 0}
    original = documents.extract

    def spy(filename, data):
        seen["max"] = max(seen["max"], len(data))
        return original(filename, data)

    monkeypatch.setattr(documents, "extract", spy)

    response = upload(client, signup(client), "huge.txt", b"x" * (4096 * 20))

    assert response.status_code == 413
    assert seen["max"] == 0, "extraction should never have been reached"


def test_a_scanned_pdf_is_a_stated_422_not_a_500(client, signup):
    """The message is the whole point: it tells someone their file is a scan
    rather than broken."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    response = upload(client, signup(client), "scan.pdf", buffer.getvalue())

    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]


def test_a_password_protected_pdf_is_a_stated_422(client, signup):
    data = make_pdf(["secret"], encrypt_with="hunter2")
    response = upload(client, signup(client), "locked.pdf", data)
    assert response.status_code == 422
    assert "password" in response.json()["detail"]


def test_an_invalid_source_type_is_refused(client, signup):
    response = upload(client, signup(client), "n.md", b"body", source_type="whatever")
    assert response.status_code == 422


# ------------------------------------------------------------------ scoping


def test_an_upload_is_not_visible_to_another_account(client, signup):
    alice = signup(client, email="alice@school.edu")
    bob = signup(client, email="bob@school.edu")

    upload(client, alice, "private.md", b"alice's lecture notes")

    assert client.get("/notes", headers=auth(bob)).json() == []


def test_an_upload_without_a_token_is_refused(client):
    response = client.post(
        "/notes/upload",
        files={"file": ("n.md", io.BytesIO(b"body"), "text/markdown")},
    )
    assert response.status_code == 401
