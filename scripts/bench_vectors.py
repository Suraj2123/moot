#!/usr/bin/env python3
"""Measure the pgvector search path against the numpy one.

Two questions this answers, and they have different answers:

  1. Is the pgvector path faster than scanning in numpy?
  2. Is it still exact?

The second matters more than the first. HNSW is approximate, so a speedup that
quietly drops true nearest neighbours is not a speedup -- it moves the retrieval
metrics for a reason that has nothing to do with retrieval quality. Every timing
below is reported next to recall against exact search, and a row with recall
under 1.0 is a row where the eval numbers would shift.

Usage:

    # demo corpus, whatever DATABASE_URL points at
    python scripts/bench_vectors.py

    # synthetic corpus at a scale worth arguing about
    DATABASE_URL=postgresql+psycopg2://... python scripts/bench_vectors.py \
        --synthetic --users 20 --per-user 1000

On SQLite only the numpy path exists, so the script reports it alone rather than
pretending there is a comparison to make.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from studylink import store  # noqa: E402
from studylink.config import load_settings  # noqa: E402
from studylink.db import connect  # noqa: E402
from studylink.embeddings import build_provider  # noqa: E402
from studylink.pgvector_support import (  # noqa: E402
    configured_ef_search,
    has_hnsw_index,
    has_vector_column,
    to_pgvector_literal,
)
from studylink.vectorstore import VectorStore  # noqa: E402

MODEL = "hash-256"
DIM = 256


def _time(fn, queries, repeats: int = 3) -> tuple[float, list[list[int]]]:
    """Median-of-repeats wall time per query, plus the ids each query returned.

    Median rather than mean: a single GC pause or an autovacuum waking up in the
    middle of a run skews a mean badly at millisecond scale.
    """
    for q in queries[: min(5, len(queries))]:  # warm caches and the query plan
        fn(q)

    timings = []
    results: list[list[int]] = []
    for attempt in range(repeats):
        start = time.perf_counter()
        run = [fn(q) for q in queries]
        timings.append((time.perf_counter() - start) / len(queries) * 1000)
        if attempt == 0:
            results = run
    return float(np.median(timings)), results


def _recall(got: list[list[int]], truth: list[list[int]]) -> float:
    """Fraction of the true top-k that the approximate path actually returned."""
    scores = [
        len(set(a) & set(b)) / len(b) if b else 1.0 for a, b in zip(got, truth)
    ]
    return float(np.mean(scores)) if scores else 1.0


def build_synthetic(conn, users: int, per_user: int, seed: int = 0) -> list[int]:
    """A corpus big enough for the numbers to mean something.

    Random unit vectors, not real embeddings: this measures the storage and
    search layer, and real text would only add chunking noise to a number that is
    about how fast rows can be ranked.
    """
    rng = np.random.default_rng(seed)
    print(f"building {users * per_user:,} vectors across {users} users ...")

    conn.execute(text("DELETE FROM embeddings"))
    conn.execute(text("DELETE FROM chunks"))
    conn.commit()

    user_ids = [store.create_user(conn, email=f"bench{i}@example.edu") for i in range(users)]
    native = has_vector_column(conn)
    chunk_id = 0

    for user_id in user_ids:
        course = store.upsert_course(conn, str(user_id), "Bench", "B", user_id=user_id)
        note = store.create_note(conn, "bench", "bench body", course_id=course, user_id=user_id)

        chunk_rows, embedding_rows = [], []
        for _ in range(per_user):
            chunk_id += 1
            vector = rng.normal(size=DIM).astype(np.float32)
            vector /= np.linalg.norm(vector)
            chunk_rows.append(
                {"id": chunk_id, "u": user_id, "n": note, "o": 0,
                 "t": "bench chunk", "cs": 180, "co": 40}
            )
            row = {"ot": "chunk", "oid": chunk_id, "m": MODEL, "d": DIM,
                   "v": vector.tobytes()}
            if native:
                row["e"] = to_pgvector_literal(vector)
            embedding_rows.append(row)

        conn.execute(
            text("INSERT INTO chunks (id, user_id, note_id, ordinal, text, chunk_size, "
                 "chunk_overlap) VALUES (:id, :u, :n, :o, :t, :cs, :co)"),
            chunk_rows,
        )
        columns = "owner_type, owner_id, model, dim, vector" + (", embedding" if native else "")
        values = ":ot, :oid, :m, :d, :v" + (", :e" if native else "")
        conn.execute(text(f"INSERT INTO embeddings ({columns}) VALUES ({values})"), embedding_rows)
        conn.commit()

    if conn.dialect.name == "postgresql":
        conn.execute(text("ANALYZE embeddings"))
        conn.execute(text("ANALYZE chunks"))
    return user_ids


EF_SEARCH_MAX = 1000  # pgvector's own upper bound on the GUC


def _ef_sweep(conn, vector_store, queries, truth, user_id, top_k) -> None:
    """Show what each increment of recall costs, and whether 1.0 is reachable.

    On a multi-tenant table it often is not. HNSW gathers `ef_search` candidates
    across every tenant's vectors and the tenant predicate is applied afterwards,
    so with N tenants the effective candidate pool for one of them is roughly
    ef_search/N. Since ef_search is capped, enough tenants make full recall
    unreachable at any setting -- and the last row of this sweep is what says so.
    """
    print()
    print("hnsw.ef_search sweep")
    print(f"{'ef_search':>16}{'ms/query':>11}{'recall@k':>11}")

    def run(label):
        ms, ids = _time(
            lambda q: [oid for oid, _ in
                       vector_store._search_native(q, "chunk", MODEL, user_id, top_k, ())],
            queries,
        )
        print(f"{label:>16}{ms:>11.2f}{_recall(ids, truth):>11.3f}")

    for ef in (40, 100, 200, 400, 800, EF_SEARCH_MAX):
        conn.execute(text(f"SET hnsw.ef_search = {ef}"))
        run(str(ef))

    # The row that matters: exact search inside Postgres, index left unused.
    conn.execute(text("SET enable_indexscan = off"))
    run("index bypassed")
    conn.execute(text("SET enable_indexscan = on"))
    print()
    print("If 'index bypassed' is both faster and at recall 1.000, the index is")
    print("costing accuracy and latency at this scale and should not be used.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true",
                        help="generate a corpus instead of using whatever is in the database")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--per-user", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--ef-sweep", action="store_true",
                        help="sweep hnsw.ef_search and show what recall costs in latency")
    args = parser.parse_args()

    settings = load_settings()
    conn = connect(settings.sqlalchemy_url)
    backend = conn.dialect.name
    native = has_vector_column(conn)

    if args.synthetic:
        user_ids = build_synthetic(conn, args.users, args.per_user)
        user_id = user_ids[0]
    else:
        user_id = store.get_or_create_default_user(conn)

    vector_store = VectorStore(conn)
    total = vector_store.count("chunk", MODEL, user_id)
    if total == 0:
        print("No chunk embeddings for this user. Run scripts/seed_demo.py first, "
              "or pass --synthetic.")
        return 1

    rng = np.random.default_rng(12345)
    if args.synthetic:
        queries = []
        for _ in range(args.queries):
            v = rng.normal(size=DIM).astype(np.float32)
            queries.append(v / np.linalg.norm(v))
    else:
        provider = build_provider(settings.embedding_provider, settings.embedding_model)
        prompts = ["gradient descent learning rate", "alliance system 1914",
                   "regularization overfitting", "dataset baseline model",
                   "marshall plan reconstruction"]
        queries = list(provider.embed(prompts * ((args.queries // len(prompts)) + 1)))[: args.queries]

    print()
    print(f"backend        {backend}")
    print(f"corpus         {total:,} vectors for the benchmarked user")
    print(f"queries        {len(queries)}, top_k={args.top_k}")
    print(f"native column  {native}")
    if native:
        print(f"hnsw index     {has_hnsw_index(conn)}   ef_search={configured_ef_search()}")
    print()

    def numpy_path(q):
        return [oid for oid, _ in
                vector_store._search_numpy(q, "chunk", MODEL, user_id, args.top_k, ())]

    numpy_ms, numpy_ids = _time(numpy_path, queries)

    # numpy is exact, so it is the ground truth every other row is scored against.
    print(f"{'path':<34}{'ms/query':>10}{'recall@k':>11}")
    print(f"{'numpy (exact, in Python)':<34}{numpy_ms:>10.2f}{1.0:>11.3f}")

    if native:
        def native_path(q):
            return [oid for oid, _ in
                    vector_store._search_native(q, "chunk", MODEL, user_id, args.top_k, ())]

        native_ms, native_ids = _time(native_path, queries)
        print(f"{'pgvector (in the database)':<34}{native_ms:>10.2f}"
              f"{_recall(native_ids, numpy_ids):>11.3f}")

        speedup = numpy_ms / native_ms if native_ms else float("inf")
        print()
        print(f"pgvector is {speedup:.2f}x the numpy path at this scale.")
        if _recall(native_ids, numpy_ids) < 1.0:
            print("Recall is below 1.0: the approximate index is dropping true "
                  "neighbours. Raise HNSW_EF_SEARCH before trusting the eval numbers.")

        if args.ef_sweep:
            _ef_sweep(conn, vector_store, queries, numpy_ids, user_id, args.top_k)
    else:
        print()
        print("No native vector column, so there is nothing to compare against. "
              "Point DATABASE_URL at Postgres with pgvector to get the second row.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
