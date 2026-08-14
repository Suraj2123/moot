"""Splitting notes into embeddable chunks.

Chunk size is one of the few retrieval knobs that reliably moves the metrics, so
this module is deliberately small and parameterised: `scripts/run_eval.py` sweeps
over `chunk_size`/`chunk_overlap` and re-indexes for each candidate.

The strategy is paragraph-aware greedy packing: keep appending paragraphs until
the word budget is hit, then start a new chunk that re-includes the tail of the
previous one. Paragraph boundaries matter for notes because a heading plus its
bullets is usually one coherent idea, and splitting mid-list produces chunks
that embed to nothing in particular.
"""

from __future__ import annotations

import re

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WORD = re.compile(r"\S+")


def _paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in _PARAGRAPH_SPLIT.split(text)]
    return [p for p in parts if p]


def _word_count(text: str) -> int:
    return len(_WORD.findall(text))


def _tail_words(text: str, n: int) -> str:
    if n <= 0:
        return ""
    words = _WORD.findall(text)
    return " ".join(words[-n:])


def _split_long_paragraph(paragraph: str, chunk_size: int, overlap: int) -> list[str]:
    """Hard-split a paragraph that is longer than a whole chunk on its own."""
    words = _WORD.findall(paragraph)
    step = max(1, chunk_size - overlap)
    pieces = []
    for start in range(0, len(words), step):
        piece = words[start : start + chunk_size]
        if piece:
            pieces.append(" ".join(piece))
        if start + chunk_size >= len(words):
            break
    return pieces


def chunk_text(text: str, chunk_size: int = 180, chunk_overlap: int = 40) -> list[str]:
    """Split `text` into overlapping chunks of roughly `chunk_size` words."""
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_words = 0

    for paragraph in _paragraphs(text):
        para_words = _word_count(paragraph)

        if para_words > chunk_size:
            flush()
            chunks.extend(_split_long_paragraph(paragraph, chunk_size, chunk_overlap))
            continue

        if current_words + para_words > chunk_size:
            carry = _tail_words("\n\n".join(current), chunk_overlap)
            flush()
            if carry:
                current.append(carry)
                current_words = _word_count(carry)

        current.append(paragraph)
        current_words += para_words

    flush()
    return [c for c in chunks if c.strip()]
