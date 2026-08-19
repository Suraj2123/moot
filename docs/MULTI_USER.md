# The multi-user data model

moot started as a single-user app: one SQLite file, one person, no notion of
ownership. Hosting it changes that — the phone talks to a shared server, and the
worst possible bug in a study app is showing one student another student's notes.

This document is the contract. If you are adding a table or a query, the rules
here are what keep that bug from shipping.

## The rule

**Every row that holds user content carries a `user_id`, and every query that
reads it filters on `user_id`.** No exceptions, no "this endpoint is internal
so it's fine".

| Table | Owner column | Notes |
|---|---|---|
| `users` | — | The identity itself. `apple_sub` is the Sign in with Apple subject; null for locally-created users. |
| `courses` | `user_id` | `UNIQUE(user_id, canvas_id)` — see below |
| `assignments` | `user_id` | Also reachable via `course_id`, but stored directly so a query never needs the join |
| `notes` | `user_id` | |
| `chunks` | `user_id` | Denormalised from `notes` |
| `eval_labels` | `user_id` | Labels are per-person judgements, not global truth |
| `work_sessions` | `user_id` | `work_messages` inherits via `session_id` |
| `embeddings` | *(none)* | Ownership comes from the row the vector describes — see below |

## Four decisions worth understanding

### 1. Canvas ids are only unique *within* a user

The original schema had `courses.canvas_id TEXT UNIQUE`. Two students in the same
class both have Canvas course `101`, so that constraint would have collapsed them
into one row, and the second student's sync would have silently overwritten the
first's course name.

It is now `UNIQUE(user_id, canvas_id)`. `tests/test_isolation.py::test_same_canvas_id_is_two_rows_for_two_users`
pins this down.

The knock-on effect matters: **a null `user_id` breaks upsert idempotency.**
SQLite treats NULLs as distinct in a unique index, so `ON CONFLICT(user_id, canvas_id)`
never fires for unowned rows, and every re-sync inserts duplicates. That is why
`user_id` is populated at insert time rather than backfilled later.

### 2. Chunks carry a denormalised `user_id`

A chunk's owner is derivable — `chunks → notes → user_id`. It is stored anyway.

Retrieval filters the chunk set on *every single query*, and that filter runs
against the largest table in the database. Paying a join for a value that can
never change independently of its parent is the wrong trade. `store.replace_chunks`
copies the owner from the note, so the two cannot drift.

### 3. Embeddings have no `user_id` — deliberately

The `embeddings` table is keyed by `(owner_type, owner_id, model)` and holds no
owner column. Ownership is proven by joining to the table named by `owner_type`
(`chunks` or `assignments`), which `vectorstore._OWNER_TABLES` maps.

The reason is that a vector is meaningless without the row it describes. Giving
it an independent owner column creates a second source of truth that can
disagree with the first — and a disagreement there means serving one user's
vectors under another user's id, which is exactly the failure being designed out.
The join makes that state unrepresentable.

### 4. Cross-user access raises; the API returns 404

`store.get_note(conn, note_id, user_id)` returns `None` for a note belonging to
someone else. That is correct for the storage layer, where absence is ordinary,
but it is wrong as a whole-system answer: it means a caller that supplied a
foreign id gets the same response as one that supplied a nonexistent id, and a
real isolation bug looks identical to an empty database.

So ids that arrive from outside go through `errors.assert_owned` first, which
raises `CrossUserAccessError`. At the API boundary that becomes a **404, not a
403** — a 403 confirms the row exists and belongs to someone else, and that fact
is precisely what is being protected. The full detail, including both user ids,
goes to the server log.

## Adding a table

1. Add `user_id INTEGER REFERENCES users(id) ON DELETE CASCADE`.
2. Add an index on `user_id` — it is in the WHERE clause of every read.
3. Make any uniqueness constraint include `user_id`.
4. Add the read and write functions to `store.py` with `user_id` as a **required**
   parameter. Not optional, not defaulted to `None`: a default is how this rots.
5. Add a case to `tests/test_isolation.py`.

## Verifying the isolation actually holds

The tests in `tests/test_isolation.py` are written so that Alice and Bob have
notes on the *same topic* with near-identical text. If scoping breaks anywhere,
retrieval ranks the other user's note highly and the assertions fail. Two users
with unrelated corpora would pass by luck.

To confirm the tests still have teeth, break the scoping on purpose and watch
them fail:

```bash
# Remove the user filter from store.list_notes, then:
python -m pytest tests/test_isolation.py -q     # expect failures
```

A green isolation suite after a change to any query is the only evidence that
change is safe.

## What day 1 deliberately left out

- **Authentication.** `UserContext.local()` is the only constructor in use; every
  request is the same local user. Day 3 adds Sign in with Apple and makes the
  context come from a verified token.
- **`NOT NULL` on `user_id`.** The columns are nullable so the backfill migration
  can run against an existing database. Once no unowned rows remain, tighten it.
- **Row-level security.** Postgres can enforce this in the database rather than
  in application code. Worth doing on day 2, when the storage engine changes.
