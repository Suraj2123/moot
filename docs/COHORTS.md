# Course cohorts

Cohorts are opt-in shared spaces for one class section and term. They are separate
from personal Canvas courses. Matching course names or Canvas IDs never joins
accounts, and joining never shares existing notes.

## Using cohorts

Open **Class cohorts** from the menu. Create a cohort with a name and term, or
check an invitation and explicitly confirm joining. Invitations also work as
`/app/?cohort_invite=CODE` links; signing in preserves the invitation.

Set your contributor display name, then use **Private · Share** on an individual
note in **Notes and folders**. Select a cohort and save. Each note can be shared
into one cohort; choosing Private revokes sharing. Classmates see the note and
your display name, not your email, personal folder, or personal Canvas course ID.
Edits to a shared note remain shared; normal indexing handles content changes.

In chat, **Answer from** defaults to your private library (including notes you own
and have shared). Selecting a cohort adds that cohort's shared notes. Changing
scope starts a fresh conversation. Citation chips show contributor names and
open a read-only source whose authorization is checked again. The cohort screen
also offers a search across your own notes plus that cohort.

Notes awaiting their owner's indexing job can be read immediately but may not be
searchable yet. The shared-note list shows this state and has a Refresh button.

## Membership and moderation

- The creator is the administrator (`role=instructor` internally). This is an app
  permission, not a claim of verified instructor status.
- Only the administrator can rotate invites, remove members, remove notes from
  the pool, or transfer administration to another active member.
- Only administrators receive the current invite code through the API.
- Removal from the pool never deletes the owner's note. The owner can choose to
  share it again; moderation is removal, not a permanent content ban.
- Leaving makes your shared notes private. You may rejoin with a valid invite,
  but notes do not automatically become shared again.
- Removed members cannot rejoin by invitation, including after code rotation.
  This release does not include an administrator unban operation.
- Administrators must transfer administration before leaving; a sole member who
  is administrator must invite a successor first.

Unsharing, leaving, and removal affect subsequent retrieval and source reads
without re-embedding. Already delivered answers remain in the reader's current
conversation; content already sent to an in-flight generation cannot be recalled.
Changing scope clears the UI conversation rather than carrying cohort context
into a private conversation. These controls cannot erase copies already read.

## Read and write boundaries

`studylink/cohorts.py` owns membership operations and the pooled SQL predicates.
All operations require the actor's user ID. A pooled read requires a selected
cohort and includes only the actor's own notes or explicitly shared notes whose
reader and contributor both have active membership in that cohort.

Vector search (native PostgreSQL and NumPy), chunk loading, note loading, chat,
agent tool search, and shared-source viewing use that policy. Private store reads
and all note edits remain owner-scoped. Membership is checked on every pooled
request; SQL also checks current membership. Inaccessible cohorts and notes
return 404. An active member attempting an administrator action receives a
correctable 400 error. Membership transitions and sharing serialize through a
cohort write lock; permission checks occur before updates are committed.

## API additions

All endpoints require authentication. Response caching remains disabled.

| Operation | Endpoint | Input |
|---|---|---|
| List/create cohorts | `GET /cohorts`, `POST /cohorts` | Create: `{name, term}` |
| Read cohort | `GET /cohorts/{id}` | — |
| Preview/join invitation | `POST /cohort-invites/preview`, `POST /cohort-invites/join` | `{code}` |
| Rotate invitation | `POST /cohorts/{id}/invite` | — |
| List/remove members | `GET /cohorts/{id}/members`, `DELETE /cohorts/{id}/members/{user_id}` | — |
| Leave | `DELETE /cohorts/{id}/membership` | — |
| Transfer administration | `POST /cohorts/{id}/transfer` | `{user_id}` |
| Share/unshare note | `PATCH /notes/{id}/sharing` | `{cohort_id: number or null}` |
| List/read shared notes | `GET /cohorts/{id}/notes`, `GET /cohorts/{id}/notes/{note_id}` | — |
| Moderate note | `DELETE /cohorts/{id}/notes/{note_id}` | — |
| Set contributor name | `PATCH /auth/profile` | `{display_name}` |

`GET /search` accepts optional `cohort_id`. `/ask`, `/ask/stream`, and
`/work-session` accept optional `cohort_id` in their request body. Omitting it
preserves private-only behavior. Answer sources and retrieval matches include
`contributor_name` and `cohort_id`; contributor name is null for the caller's own
notes. Both streaming source events and final answer payloads carry attribution.
`GET /auth/me` also returns the caller's display name.

## Migration and running

Back up the existing database before upgrading. Stop application writes, use the
same database configuration as the running service, then run:

```sh
.venv/bin/python -m alembic upgrade head
cd web
npm ci
npm run build
```

Migration `0016` follows `0015`, creates cohorts and memberships, and adds
`notes.visibility` (non-null, default `private`) and `notes.shared_cohort_id`.
Every old note remains private. SQLite uses Alembic batch mode for constraints;
PostgreSQL uses the same portable schema. Downgrading to `0015` removes cohort
metadata and sharing state while retaining notes. Do not downgrade a live shared
instance without first accounting for that loss of membership metadata.

Start the API normally; the production build is served at `/app/`. Development
Vite proxies `/cohorts` and `/cohort-invites` alongside existing API paths.
No live deployment is performed by this change.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_cohorts.py tests/test_api_cohorts.py tests/test_isolation.py -q
# A disposable Postgres database with pgvector enables native checks:
STUDYLINK_TEST_POSTGRES_URL=postgresql://... .venv/bin/python -m pytest tests/test_pgvector.py tests/test_migrations.py tests/test_cohorts.py -q
# Local evaluation on a deliberately seeded database:
EMBEDDING_PROVIDER=hash .venv/bin/python scripts/run_eval.py --json private-eval.json
EMBEDDING_PROVIDER=hash .venv/bin/python scripts/run_eval.py --user-id 2 --cohort-id 1 --json pooled-eval.json
```

The cohort evaluator uses only authorized notes and the selected user's personal
assignments. Shared notes must already be indexed by their owners. Labels with
ambiguous titles are reported unresolved rather than assigned to an arbitrary
contributor. Cohort evaluation supports ordinary ranking, not sweeps or LLM judge
mode. User ID override is a local CLI operation requiring database access; it is
never an API authentication override.

### Verification record (2026-09-26)

- Original baseline: 768 tests passed, 23 skipped.
- Final full suite: 829 tests passed, 23 skipped (109 seconds).
- Fresh SQLite migration, upgrade over existing notes and the seeded demo,
  downgrade/re-upgrade, and schema consistency checks passed.
- The private hash-provider evaluation matched its pre-change JSON exactly:
  recall@5 1.0, precision@5 0.4, MRR 1.0, NDCG@5 0.9636.
- Pooled CLI evaluation used a second account with personal assignments and no
  notes, reading 12 shared notes and 20 labels from one cohort. It produced the
  same metrics, with no unresolved labels.
- Removing the selected-cohort filter in a disposable source copy caused the
  adversarial test to retrieve a note from another cohort and fail. Restoring
  the filter made the test pass. No broken filter remains in the working tree.
- Production frontend TypeScript check and Vite build passed.
- Multi-account API tests cover sharing, revocation, access denial, moderation,
  and both streamed and non-streamed attribution using a deterministic fake
  model. Agent tool-search attribution and citation eligibility also pass.
- Live PostgreSQL/pgvector tests require `STUDYLINK_TEST_POSTGRES_URL`, which was
  not available. Live generation requires provider credentials, which were not
  available. Browser verification was blocked because the browser tool reported
  declined permission to access the local app; visual UI behavior is unverified.
