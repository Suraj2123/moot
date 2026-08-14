"""Note <-> assignment matching, with explanations.

Retrieval is chunk-level; results are reported at note level. A note scores as
its single best-matching chunk (max-pooling rather than averaging) because a
long note that covers one relevant topic among five should still surface for
that topic -- averaging would bury it.

Every match carries `Evidence`: the sentence that drove the match and the terms
the note and the assignment share. That is the difference between "here are five
notes, trust us" and a result a student can sanity-check in two seconds.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

import numpy as np

from . import store
from .config import RetrievalConfig
from .embeddings import EmbeddingProvider, tokenize
from .models import Assignment, AssignmentMatch, Evidence, Note, NoteMatch
from .vectorstore import VectorStore

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def explain(query_text: str, chunk_text: str, chunk_ordinal: int = 0, max_terms: int = 6) -> Evidence:
    """Build the human-readable "why did this match?" for one chunk.

    Deliberately lexical and deterministic: no second model call, nothing to
    hallucinate. It answers "what do these two texts literally have in common",
    which is a claim the student can verify by looking at the highlighted words.
    Semantic matches with no shared vocabulary produce an empty term list -- and
    that absence is itself useful signal about the match.
    """
    query_terms = set(tokenize(query_text))
    chunk_tokens = tokenize(chunk_text)

    counts: dict[str, int] = {}
    for token in chunk_tokens:
        if token in query_terms:
            counts[token] = counts.get(token, 0) + 1
    overlapping = [term for term, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:max_terms]

    # The snippet is the sentence with the densest overlap -- the line a student
    # would point at if asked why the note is relevant.
    best_sentence = ""
    best_hits = -1
    for sentence in _sentences(chunk_text):
        hits = sum(1 for token in tokenize(sentence) if token in query_terms)
        if hits > best_hits:
            best_hits = hits
            best_sentence = sentence
    if not best_sentence:
        best_sentence = chunk_text[:280]

    return Evidence(
        snippet=best_sentence[:400],
        overlapping_terms=overlapping,
        chunk_ordinal=chunk_ordinal,
    )


def calibrate_confidence(
    score: float, evidence: Evidence, query_text: str, config: RetrievalConfig
) -> float:
    """Map a raw cosine score onto a 0-1 confidence for display.

    Two signals, deliberately kept simple and inspectable:

      * where the cosine score sits between the display threshold and the
        "clearly relevant" ceiling, and
      * how much of the assignment's vocabulary the matched chunk actually uses.

    The lexical term is weighted lightly (25%). It is there to catch the failure
    mode where a vector model returns a confident-looking score for a note that
    shares no concepts with the assignment -- a case where the score alone would
    mislead. Both inputs are shown in the UI alongside this number, so the
    student can disagree with the weighting.
    """
    span = max(config.confidence_ceiling - config.score_threshold, 1e-6)
    normalized = (score - config.score_threshold) / span
    normalized = float(np.clip(normalized, 0.0, 1.0))

    query_terms = set(tokenize(query_text))
    overlap_ratio = len(evidence.overlapping_terms) / max(len(query_terms), 1)
    overlap_ratio = float(np.clip(overlap_ratio * 4, 0.0, 1.0))  # 25% coverage reads as full

    return round(0.75 * normalized + 0.25 * overlap_ratio, 3)


class Retriever:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: EmbeddingProvider,
        config: RetrievalConfig,
    ) -> None:
        self.conn = conn
        self.provider = provider
        self.config = config
        self.vectors = VectorStore(conn)

    # ------------------------------------------------------------------ helpers

    def _best_chunk_per_note(
        self, query_vector: np.ndarray, candidate_pool: int
    ) -> list[tuple[int, int, float]]:
        """Return (note_id, chunk_id, score), best chunk first, one row per note."""
        hits = self.vectors.search(
            query_vector, "chunk", self.provider.name, top_k=candidate_pool
        )
        if not hits:
            return []

        chunk_to_note = store.chunk_note_map(self.conn)
        best: dict[int, tuple[int, float]] = {}
        for chunk_id, score in hits:
            note_id = chunk_to_note.get(chunk_id)
            if note_id is None:
                continue  # chunk deleted between search and lookup
            if note_id not in best or score > best[note_id][1]:
                best[note_id] = (chunk_id, score)

        return sorted(
            ((note_id, chunk_id, score) for note_id, (chunk_id, score) in best.items()),
            key=lambda row: -row[2],
        )

    def _build_matches(
        self,
        query_text: str,
        ranked: list[tuple[int, int, float]],
        top_k: int,
        apply_threshold: bool,
    ) -> list[NoteMatch]:
        note_ids = [note_id for note_id, _, _ in ranked[: top_k * 2]]
        notes = store.get_notes(self.conn, note_ids)
        chunks = store.get_chunks(self.conn, [chunk_id for _, chunk_id, _ in ranked[: top_k * 2]])

        matches: list[NoteMatch] = []
        for note_id, chunk_id, score in ranked:
            if apply_threshold and score < self.config.score_threshold:
                continue
            note = notes.get(note_id)
            chunk = chunks.get(chunk_id)
            if note is None or chunk is None:
                continue
            evidence = explain(query_text, chunk.text, chunk.ordinal)
            matches.append(
                NoteMatch(
                    note=note,
                    score=score,
                    confidence=calibrate_confidence(score, evidence, query_text, self.config),
                    evidence=evidence,
                    matched_chunk=chunk,
                )
            )
            if len(matches) >= top_k:
                break
        return matches

    # -------------------------------------------------------------------- public

    def notes_for_assignment(
        self,
        assignment: Assignment,
        top_k: Optional[int] = None,
        apply_threshold: bool = True,
    ) -> list[NoteMatch]:
        """Top-N notes relevant to an assignment. The core of the app."""
        top_k = top_k or self.config.top_k
        query_vector = self.vectors.get("assignment", assignment.id, self.provider.name)
        if query_vector is None:
            # Assignment was added since the last reindex -- embed it on the fly
            # so the UI never shows an empty result for a stale index.
            query_vector = self.provider.embed([assignment.retrieval_text])[0]

        # Over-fetch chunks: several chunks of the same note often crowd the top
        # of the chunk ranking, and we need enough distinct notes to fill top_k.
        ranked = self._best_chunk_per_note(query_vector, candidate_pool=max(top_k * 8, 40))
        return self._build_matches(assignment.retrieval_text, ranked, top_k, apply_threshold)

    def search_notes(
        self, query: str, top_k: int = 5, apply_threshold: bool = False
    ) -> list[NoteMatch]:
        """Free-text semantic search over notes. Backs the agent's search tool."""
        if not query.strip():
            return []
        query_vector = self.provider.embed([query])[0]
        ranked = self._best_chunk_per_note(query_vector, candidate_pool=max(top_k * 8, 40))
        return self._build_matches(query, ranked, top_k, apply_threshold)

    def assignments_for_note(
        self, note: Note, top_k: int = 5, apply_threshold: bool = True
    ) -> list[AssignmentMatch]:
        """Reverse lookup: which assignments is this note relevant to?"""
        chunks = store.list_chunks(self.conn, note_id=note.id)
        if not chunks:
            return []

        # Score every assignment against every chunk of the note and keep each
        # assignment's best chunk -- the mirror image of note-level max-pooling.
        chunk_vectors = []
        kept_chunks = []
        for chunk in chunks:
            vector = self.vectors.get("chunk", chunk.id, self.provider.name)
            if vector is not None:
                chunk_vectors.append(vector)
                kept_chunks.append(chunk)
        if not chunk_vectors:
            return []

        assignment_ids, matrix = self.vectors.matrix("assignment", self.provider.name)
        if not assignment_ids:
            return []

        scores = matrix @ np.vstack(chunk_vectors).T  # (n_assignments, n_chunks)
        best_chunk_idx = np.argmax(scores, axis=1)
        best_scores = scores[np.arange(scores.shape[0]), best_chunk_idx]

        order = np.argsort(-best_scores)
        results: list[AssignmentMatch] = []
        for idx in order:
            score = float(best_scores[int(idx)])
            if apply_threshold and score < self.config.score_threshold:
                continue
            assignment = store.get_assignment(self.conn, assignment_ids[int(idx)])
            if assignment is None:
                continue
            chunk = kept_chunks[int(best_chunk_idx[int(idx)])]
            evidence = explain(assignment.retrieval_text, chunk.text, chunk.ordinal)
            results.append(
                AssignmentMatch(
                    assignment=assignment,
                    score=score,
                    confidence=calibrate_confidence(
                        score, evidence, assignment.retrieval_text, self.config
                    ),
                    evidence=evidence,
                )
            )
            if len(results) >= top_k:
                break
        return results
