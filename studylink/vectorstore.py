"""A small vector store backed by the same SQLite file as everything else.

Why not Chroma or FAISS? At MVP scale the corpus is a single semester of notes --
low thousands of chunks. An exact numpy dot product over that is sub-millisecond,
which is faster than any approximate index would be once you include its own
overhead, and it is exact, so retrieval metrics measure the retriever rather than
the index's recall loss. Adding a second storage system would also mean two
things to keep in sync when a note is edited.

The interface below (`upsert_many` / `search`) is the same shape Chroma exposes,
so swapping in an ANN index later is a change to this file only.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

import numpy as np

from .db import transaction


def _to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class VectorStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert_many(
        self,
        owner_type: str,
        owner_ids: Sequence[int],
        vectors: np.ndarray,
        model: str,
    ) -> None:
        if len(owner_ids) != len(vectors):
            raise ValueError("owner_ids and vectors must be the same length")
        rows = [
            (owner_type, int(owner_id), model, int(vector.shape[0]), _to_blob(vector))
            for owner_id, vector in zip(owner_ids, vectors)
        ]
        with transaction(self.conn):
            self.conn.executemany(
                """
                INSERT INTO embeddings (owner_type, owner_id, model, dim, vector)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_type, owner_id, model)
                DO UPDATE SET dim = excluded.dim, vector = excluded.vector
                """,
                rows,
            )

    def delete(self, owner_type: str, owner_ids: Iterable[int], model: str | None = None) -> None:
        ids = [int(i) for i in owner_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        params: list = [owner_type, *ids]
        sql = f"DELETE FROM embeddings WHERE owner_type = ? AND owner_id IN ({placeholders})"
        if model:
            sql += " AND model = ?"
            params.append(model)
        with transaction(self.conn):
            self.conn.execute(sql, params)

    def matrix(self, owner_type: str, model: str) -> tuple[list[int], np.ndarray]:
        """Load every vector of a kind into memory as one (n, dim) matrix."""
        rows = self.conn.execute(
            "SELECT owner_id, vector FROM embeddings WHERE owner_type = ? AND model = ? ORDER BY owner_id",
            (owner_type, model),
        ).fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        ids = [int(r["owner_id"]) for r in rows]
        matrix = np.vstack([_from_blob(r["vector"]) for r in rows])
        return ids, matrix

    def get(self, owner_type: str, owner_id: int, model: str) -> np.ndarray | None:
        row = self.conn.execute(
            "SELECT vector FROM embeddings WHERE owner_type = ? AND owner_id = ? AND model = ?",
            (owner_type, int(owner_id), model),
        ).fetchone()
        return _from_blob(row["vector"]) if row else None

    def search(
        self,
        query: np.ndarray,
        owner_type: str,
        model: str,
        top_k: int = 10,
        exclude_ids: Iterable[int] = (),
    ) -> list[tuple[int, float]]:
        """Exact cosine search. Vectors are stored L2-normalised, so this is a dot product."""
        ids, matrix = self.matrix(owner_type, model)
        if not ids:
            return []
        if matrix.shape[1] != query.shape[0]:
            raise ValueError(
                f"Dimension mismatch: stored vectors are {matrix.shape[1]}-d but the query is "
                f"{query.shape[0]}-d. Re-index after changing embedding models."
            )

        scores = matrix @ np.asarray(query, dtype=np.float32)
        excluded = {int(i) for i in exclude_ids}

        order = np.argsort(-scores)
        results: list[tuple[int, float]] = []
        for idx in order:
            owner_id = ids[int(idx)]
            if owner_id in excluded:
                continue
            results.append((owner_id, float(scores[int(idx)])))
            if len(results) >= top_k:
                break
        return results

    def count(self, owner_type: str, model: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings WHERE owner_type = ? AND model = ?",
            (owner_type, model),
        ).fetchone()
        return int(row["n"])
