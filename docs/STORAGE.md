# Storage and search: when exact search stops being good enough

StudyLink searches notes by comparing embeddings. There are two implementations
of that comparison and one meaning. This is the record of which is running,
why, and what would have to change to move to the next one.

## Where things stand

| | SQLite | Postgres |
|---|---|---|
| vector storage | `embeddings.vector`, a packed float32 blob | the same blob, plus a native `vector(256)` column |
| search | exact dot product in numpy | exact top-N in the database |
| index | none | none by default |

Both paths return the same rows in the same order. That is asserted directly
(`test_native_search_matches_numpy_exactly`) and again end to end in CI, which
diffs the retrieval metrics between backends and fails the build if they move.

The portable blob is the source of truth on both engines. The native column is
a derived copy that exists so Postgres can rank without shipping every vector to
Python.

## Why exact search is still the right answer

The instinct with vector search is to reach for an approximate index. On this
schema that is currently a downgrade, and the numbers are worth keeping because
the reasoning is not obvious.

Measured with `scripts/bench_vectors.py --synthetic --users 20 --per-user 1000
--ef-sweep`, 20,000 vectors, top_k=5, recall against exact search:

```
       ef_search   ms/query   recall@5
              40       2.21      0.320
             100       3.20      0.560
             200       4.59      0.760
             400       7.05      0.933
             800      11.58      1.000
            1000      13.65      1.000
  index bypassed       8.74      1.000
   numpy (exact)      11.45      1.000
```

An HNSW index is strictly dominated here. Every setting that reaches full recall
is slower than not using the index, and the setting that is faster loses nearly
half the true nearest neighbours.

The cause is post-filtering, and it is a property of the schema rather than of
pgvector. `embeddings` is multi-tenant: rows for every user live in one table,
and the query scopes to one user by joining to the table that owns the vector.
HNSW cannot apply that predicate while walking its graph. It collects
`ef_search` candidates across everyone's vectors and the predicate discards them
afterwards, so one tenant out of N effectively receives `ef_search / N`
candidates. pgvector caps `ef_search` at 1000, which means that past some number
of tenants, full recall is unreachable at any setting.

This matters more than latency. A retrieval layer that silently drops true
neighbours moves every number in the eval report for a reason that has nothing
to do with retrieval quality, and it does so without an error anywhere. The
recall column in `bench_vectors.py` exists so that this cannot happen quietly.

So migration 0004 drops the index that 0003 added. The build stays available
behind `STUDYLINK_HNSW=1`, and the tests still exercise it, so the path is
supported rather than deleted.

## What would change the answer

In rough order of likelihood:

**pgvector 0.8 or newer.** Iterative index scans keep searching the graph until
enough rows survive the filter, instead of returning a fixed candidate set and
letting the filter empty it. That is precisely the missing piece. This is the
change most likely to flip the decision, and re-running the benchmark is the way
to confirm it rather than assume it.

**A single user's corpus outgrowing an exact scan.** The relevant number is not
the size of the table, it is the size of one user's slice, because that is what
the scan touches. At a semester of notes — a few thousand chunks — exact search
is single-digit milliseconds. The benchmark will show this turning before users
notice it.

**Partitioning `embeddings` by tenant.** Each partition gets its own index and
the tenant predicate becomes partition selection rather than post-filtering,
which removes the problem at its root. It is also the largest change of the
three, and it buys nothing until one of the above is already true.

**Denormalising `user_id` onto `embeddings`.** Measured at ~20ms per query on
20k vectors, from removing the join. Deliberately not done: `docs/MULTI_USER.md`
records the decision that embeddings carry no user column, so that ownership is
always proven by joining to the row the vector describes and no code path can
return a vector without having established who owns it. Trading that for latency
is a real trade and should be made deliberately, not folded into a performance
commit.

## Re-running the measurements

```bash
# whatever DATABASE_URL points at, using the demo corpus
python scripts/bench_vectors.py

# a corpus large enough for the answer to differ, plus the recall sweep
DATABASE_URL=postgresql+psycopg2://... \
  python scripts/bench_vectors.py --synthetic --users 20 --per-user 1000 --ef-sweep
```

Read the recall column first. A row that is faster at recall below 1.000 is not
faster, it is answering a different question.

## Changing embedding provider

The native column's width is fixed when migration 0002 runs, because pgvector
needs a declared dimension. `EMBEDDING_DIM` records the choice, and it is read at
migration time rather than at query time — switching provider (hash-256 is 256
wide, voyage-3 is 1024) needs a migration that alters the column and a full
reindex, not a config change on a live database.

The portable blob has no such constraint; it stores whatever width it is given
and `dim` is a column. That asymmetry is the price of the native column, and it
is worth knowing before picking a provider.
