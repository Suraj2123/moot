from __future__ import annotations

import pytest

from studylink.chunking import chunk_text


def words(text: str) -> int:
    return len(text.split())


def test_short_text_is_one_chunk():
    assert chunk_text("A short note about entropy.", chunk_size=50, chunk_overlap=10) == [
        "A short note about entropy."
    ]


def test_empty_text_produces_no_chunks():
    assert chunk_text("", chunk_size=50, chunk_overlap=10) == []
    assert chunk_text("   \n\n  ", chunk_size=50, chunk_overlap=10) == []


def test_chunks_respect_the_word_budget():
    text = "\n\n".join(" ".join(f"word{i}" for i in range(30)) for _ in range(10))
    chunks = chunk_text(text, chunk_size=60, chunk_overlap=10)
    assert len(chunks) > 1
    # Allow the overlap carry-over on top of the budget.
    assert all(words(chunk) <= 60 + 10 for chunk in chunks)


def test_overlap_carries_context_between_chunks():
    paragraphs = [" ".join(f"p{p}w{i}" for i in range(40)) for p in range(4)]
    chunks = chunk_text("\n\n".join(paragraphs), chunk_size=60, chunk_overlap=15)
    assert len(chunks) > 1
    # The tail of chunk n should reappear at the head of chunk n+1.
    tail = chunks[0].split()[-15:]
    assert any(token in chunks[1] for token in tail)


def test_paragraph_longer_than_chunk_is_hard_split():
    long_paragraph = " ".join(f"w{i}" for i in range(500))
    chunks = chunk_text(long_paragraph, chunk_size=100, chunk_overlap=20)
    assert len(chunks) >= 5
    assert all(words(chunk) <= 100 for chunk in chunks)


def test_no_content_is_dropped_on_hard_split():
    long_paragraph = " ".join(f"w{i}" for i in range(300))
    chunks = chunk_text(long_paragraph, chunk_size=100, chunk_overlap=0)
    seen = " ".join(chunks).split()
    assert "w0" in seen and "w299" in seen


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size=50, chunk_overlap=50)
