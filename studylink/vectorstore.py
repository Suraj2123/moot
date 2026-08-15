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

from typing import Iterable, Sequence

import numpy as np
from sqlalchemy import Connection, delete, func, select

from .db import transaction
from .pgvector_support import has_vector_column, to_vector_param
from .schema import assignments, chunks, embeddings


def _to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# The embeddings table is keyed by (owner_type, owner_id) and carries no user
# column of its own -- ownership lives on the row the vector describes. Every
# read joins through this map, so there is no code path that can return a vector
# without having proved who owns it.
_OWNER_TABLES = {"chunk": chunks, "assignment": assignments}


def _owner_table(owner_type: str):
    try:
        return _OWNER_TABLES[owner_type]
    except KeyError:
        raise ValueError(
            f"Unknown owner_type {owner_type!r}. Expected one of {sorted(_OWNER_TABLES)}."
        ) from None


class VectorStore:
    def __init__(self, conn: Connection) -> None:
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
        from .store import _upsert  # local import: avoids a circular module cycle

        rows = [
            {
                "owner_type": owner_type,
                "owner_id": int(owner_id),
                "model": model,
                "dim": int(vector.shape[0]),
                "vector": _to_blob(vector),
            }
            for owner_id, vector in zip(owner_ids, vectors)
        ]
        native = has_vector_column(self.conn)
        if native:
            for row, vector in zip(rows, vectors):
                row["embedding"] = to_vector_param(vector)

        statement = _upsert(self.conn, embeddings)
        statement = statement.on_conflict_do_update(
            index_elements=[
                embeddings.c.owner_type,
                embeddings.c.owner_id,
                embeddings.c.model,
            ],
            set_={"dim": statement.excluded.dim, "vector": statement.excluded.vector}
            | ({"embedding": statement.excluded.embedding} if native else {}),
        )
        with transaction(self.conn):
            self.conn.execute(statement, rows)

    def delete(self, owner_type: str, owner_ids: Iterable[int], model: str | None = None) -> None:
        ids = [int(i) for i in owner_ids]
        if not ids:
            return
        statement = delete(embeddings).where(
            embeddings.c.owner_type == owner_type, embeddings.c.owner_id.in_(ids)
        )
        if model:
            statement = statement.where(embeddings.c.model == model)
        with transaction(self.conn):
            self.conn.execute(statement)

    def matrix(
        self, owner_type: str, model: str, user_id: int
    ) -> tuple[list[int], np.ndarray]:
        """Load one user's vectors of a kind into memory as an (n, dim) matrix."""
        owner = _owner_table(owner_type)
        rows = self.conn.execute(
            select(embeddings.c.owner_id, embeddings.c.vector)
            .join(owner, owner.c.id == embeddings.c.owner_id)
            .where(
                embeddings.c.owner_type == owner_type,
                embeddings.c.model == model,
                owner.c.user_id == user_id,
            )
            .order_by(embeddings.c.owner_id)
        ).all()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        ids = [int(row[0]) for row in rows]
        matrix = np.vstack([_from_blob(row[1]) for row in rows])
        return ids, matrix

    def get(
        self, owner_type: str, owner_id: int, model: str, user_id: int
    ) -> np.ndarray | None:
        owner = _owner_table(owner_type)
        row = self.conn.execute(
            select(embeddings.c.vector)
            .join(owner, owner.c.id == embeddings.c.owner_id)
            .where(
                embeddings.c.owner_type == owner_type,
                embeddings.c.owner_id == int(owner_id),
                embeddings.c.model == model,
                owner.c.user_id == user_id,
            )
        ).first()
        return _from_blob(row[0]) if row else None

    def search(
        self,
        query: np.ndarray,
        owner_type: str,
        model: str,
        user_id: int,
        top_k: int = 10,
        exclude_ids: Iterable[int] = (),
    ) -> list[tuple[int, float]]:
        """Exact cosine search over one user's vectors.

        Vectors are stored L2-normalised, so this is a dot product.
        """
        ids, matrix = self.matrix(owner_type, model, user_id)
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

    def count(self, owner_type: str, model: str, user_id: int) -> int:
        owner = _owner_table(owner_type)
        row = self.conn.execute(
            select(func.count())
            .select_from(embeddings)
            .join(owner, owner.c.id == embeddings.c.owner_id)
            .where(
                embeddings.c.owner_type == owner_type,
                embeddings.c.model == model,
                owner.c.user_id == user_id,
            )
        ).first()
        return int(row[0])
