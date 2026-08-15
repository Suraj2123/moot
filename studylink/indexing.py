"""Building and refreshing the vector index.

Indexing is incremental in two dimensions:

  * chunks are rebuilt only when a note's chunking parameters differ from the
    active `RetrievalConfig`, and
  * vectors are computed only for owners with no vector *under the current model
    name*.

That second point is what makes provider swaps cheap to evaluate: pointing
`EMBEDDING_PROVIDER` at a different model embeds everything fresh under a new key
while leaving the old vectors intact, so you can A/B two providers without
re-ingesting notes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from . import store
from .chunking import chunk_text
from .config import RetrievalConfig
from .embeddings import EmbeddingProvider
from .vectorstore import VectorStore


@dataclass
class IndexStats:
    notes_chunked: int = 0
    chunks_written: int = 0
    chunk_vectors: int = 0
    assignment_vectors: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{self.notes_chunked} note(s) chunked into {self.chunks_written} chunk(s); "
            f"embedded {self.chunk_vectors} chunk(s) and {self.assignment_vectors} assignment(s)"
        )


class Indexer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: EmbeddingProvider,
        config: RetrievalConfig,
        user_id: int,
    ) -> None:
        self.conn = conn
        self.provider = provider
        self.config = config
        self.user_id = user_id
        self.vectors = VectorStore(conn)

    # ------------------------------------------------------------------ chunking

    def _needs_rechunk(self, note_id: int) -> bool:
        rows = self.conn.execute(
            "SELECT chunk_size, chunk_overlap, COUNT(*) AS n FROM chunks WHERE note_id = ? "
            "GROUP BY chunk_size, chunk_overlap",
            (note_id,),
        ).fetchall()
        if len(rows) != 1:
            return True  # no chunks at all, or a mix of parameter sets
        return (
            int(rows[0]["chunk_size"]) != self.config.chunk_size
            or int(rows[0]["chunk_overlap"]) != self.config.chunk_overlap
        )

    def chunk_notes(self, force: bool = False) -> tuple[int, int]:
        notes_chunked = 0
        chunks_written = 0
        for note in store.list_notes(self.conn, self.user_id):
            if not force and not self._needs_rechunk(note.id):
                continue
            texts = chunk_text(
                f"{note.title}\n\n{note.body}",
                chunk_size=self.config.chunk_size,
                chunk_overlap=self.config.chunk_overlap,
            )
            store.replace_chunks(
                self.conn, note.id, texts, self.config.chunk_size, self.config.chunk_overlap
            )
            notes_chunked += 1
            chunks_written += len(texts)
        return notes_chunked, chunks_written

    # ----------------------------------------------------------------- embedding

    def _embed_missing(self, owner_type: str, items: list[tuple[int, str]]) -> int:
        if not items:
            return 0
        existing = {
            int(r["owner_id"])
            for r in self.conn.execute(
                "SELECT owner_id FROM embeddings WHERE owner_type = ? AND model = ?",
                (owner_type, self.provider.name),
            )
        }
        pending = [(owner_id, text) for owner_id, text in items if owner_id not in existing]
        if not pending:
            return 0

        ids = [owner_id for owner_id, _ in pending]
        vectors = self.provider.embed([text for _, text in pending])
        self.vectors.upsert_many(owner_type, ids, vectors, self.provider.name)
        return len(ids)

    def embed_chunks(self) -> int:
        chunks = store.list_chunks(self.conn, self.user_id)
        return self._embed_missing("chunk", [(c.id, c.text) for c in chunks])

    def embed_assignments(self) -> int:
        assignments = store.list_assignments(self.conn, self.user_id)
        return self._embed_missing(
            "assignment", [(a.id, a.retrieval_text) for a in assignments]
        )

    # --------------------------------------------------------------------- entry

    def reindex(self, force: bool = False) -> IndexStats:
        """Bring the index up to date with the notes, assignments, and config."""
        notes_chunked, chunks_written = self.chunk_notes(force=force)
        if force:
            # Drop existing vectors under this model so they are recomputed.
            chunk_ids = [c.id for c in store.list_chunks(self.conn, self.user_id)]
            assignment_ids = [a.id for a in store.list_assignments(self.conn, self.user_id)]
            self.vectors.delete("chunk", chunk_ids, self.provider.name)
            self.vectors.delete("assignment", assignment_ids, self.provider.name)

        return IndexStats(
            notes_chunked=notes_chunked,
            chunks_written=chunks_written,
            chunk_vectors=self.embed_chunks(),
            assignment_vectors=self.embed_assignments(),
        )

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a free-text query with the same provider used for the index."""
        return self.provider.embed([text])[0]
