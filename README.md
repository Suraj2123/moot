# StudyLink

A notes app that connects to Canvas, automatically maps your notes and lecture
material to the assignments they're relevant to, and turns matched notes into a
working draft or study plan — powered by retrieval (RAG) and an AI agent, not
manual searching.

## Why

Notes end up scattered across a semester — different courses, different formats,
no link back to the assignment they actually apply to. StudyLink closes that gap:
pull assignments from Canvas, embed your notes, semantically match them, **measure
how good the matching actually is**, and use an agent to synthesise matched notes
plus assignment context into something you can start working from.

## Quick start

Runs end to end with no API keys at all — the default embedding provider is a
deterministic offline baseline, so the demo and the evaluation are reproducible on
any machine.

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: add Canvas / Anthropic / Voyage keys
python scripts/seed_demo.py   # 2 courses, 5 assignments, 12 notes, 20 labelled pairs
streamlit run app.py
```

Measure retrieval quality:

```bash
python scripts/run_eval.py            # score the current configuration
python scripts/run_eval.py --sweep    # grid-search chunking and thresholds
python scripts/run_eval.py --judge    # validate the LLM judge against hand labels
```

Or drive it headlessly:

```bash
uvicorn studylink.api:api --reload    # then GET /assignments/1/matches
```

## What's here

**Canvas integration** — `studylink/canvas.py` pulls courses and assignments over
the REST API with a personal access token, following Canvas's link-header
pagination and stripping HTML descriptions to plain text. Sync is on demand and
idempotent: re-running it updates rows in place.

**Notes ingestion** — paste or upload markdown/plain text, tagged by course, with
lecture transcripts as a first-class document type through the same pipeline.

**Semantic matching** — notes are chunked, embedded, and matched against
assignment descriptions by exact cosine similarity. Retrieval happens at chunk
level and reports at note level (max-pooled), so a long note that covers one
relevant topic still surfaces for it. Reverse lookup works too: given a note, see
which assignments it bears on.

**Explainability** — every match shows *why*: the concepts the note and the
assignment share, and the specific sentence that drove the score. Deterministic
and lexical, so there is nothing to hallucinate. In the demo corpus this
immediately exposes a match that scored 0.495 on the words `effect`, `under`,
`rate` — a respectable-looking number with no topical content behind it.

**Measured retrieval quality** — a hand-labelled set (`data/eval/labels.json`,
positives *and* deliberate distractors), precision@k / recall@k / MRR / nDCG /
MAP, a configuration sweep that reindexes per candidate, and an LLM judge that
reports Cohen's kappa against the hand labels before you're allowed to trust it.

**Agent work sessions** — pick an assignment, get matched notes pulled
automatically, and an agent produces a study outline, draft skeleton, or concept
summary, citing `[N<id>]` inline. A traceability check flags any note id the agent
cited that it was never given.

## Measured results

On the seeded demo corpus (20 labelled pairs, 5 assignments, hash baseline
provider), from `python scripts/run_eval.py --sweep --top-k 3`:

| config | recall@3 | precision@3 | MRR | nDCG@3 |
|---|---|---|---|---|
| chunk=120/0 | **1.000** | 0.667 | 1.000 | 0.984 |
| chunk=120/40 | 0.900 | 0.600 | 1.000 | 0.923 |
| chunk=180/40 | 0.833 | 0.533 | 1.000 | 0.876 |
| chunk=300/40 | 0.833 | 0.533 | 1.000 | 0.876 |

Smaller chunks win on this corpus — the notes are topically dense, so a 300-word
chunk averages several ideas into one vector. `precision@k` is capped below 1.0
whenever an assignment has fewer than *k* relevant notes, so it is comparable
across configurations but is not an absolute score. Reproduce with the commands
above.

## Architecture

```
db / store        SQLite schema and CRUD
chunking          note text -> embeddable chunks (paragraph-aware, overlapping)
embeddings        pluggable providers: hash | voyage | sentence-transformers
vectorstore       vectors in SQLite, exact cosine search over numpy
indexing          keeps chunks and vectors in sync with the active config
retrieval         note <-> assignment matching, with evidence
agent             work-session synthesis with citation checking
evaluation        labelled set, metrics, LLM judge, config sweep
service           the facade the UI, API, and scripts all drive
```

| Layer | Choice | Why |
|---|---|---|
| Canvas | REST + PAT | On-demand sync is enough for v1; no webhook infrastructure |
| Embeddings | Provider interface, 3 impls | The eval has to compare providers on one labelled set |
| Vector store | SQLite + numpy | Exact search on a semester of notes is sub-millisecond, and one file to back up. The interface matches Chroma's, so swapping is one file |
| Agent | Anthropic Messages API, hand-written tool loop | The tool closes over a per-request retriever; a global would be worse than 30 lines of loop |
| Storage | SQLite | Single user, single file, no server |
| Frontend | Streamlit | Function over polish for v1 |

## Configuration

All via environment (see `.env.example`). Nothing is hardcoded and no secret is
written to the database or logged.

| Variable | Purpose |
|---|---|
| `CANVAS_API_URL`, `CANVAS_API_TOKEN` | Canvas sync |
| `EMBEDDING_PROVIDER` | `hash` (default, offline) / `voyage` / `sentence-transformers` |
| `VOYAGE_API_KEY` | If using Voyage |
| `ANTHROPIC_API_KEY` | Agent and LLM judge only; retrieval and metrics work without it |
| `CHUNK_SIZE`, `CHUNK_OVERLAP`, `TOP_K`, `SCORE_THRESHOLD` | Retrieval tuning |

## Tests

```bash
python -m pytest tests -q
```

66 tests, no network, no API keys. Eighteen more cover the Postgres and pgvector
paths and skip unless you point them at a server:

```bash
STUDYLINK_TEST_POSTGRES_URL=postgresql+psycopg2://... python -m pytest tests -q
```

CI runs both, and diffs the retrieval metrics between the two backends.

## Design notes

- `docs/STORAGE.md` -- how vectors are stored and searched on each backend, why
  there is no approximate index, and the measurements that would change that.
- `docs/MULTI_USER.md` -- how rows are scoped to one user, and the four
  decisions behind it.

## Learning path

`docs/LEARNING_PATH.md` turns this repo into a course: each stage names the
decision behind a piece of the system, gives you a rebuild-it-yourself task and a
stretch task, and includes questions you should be able to answer before running
the code.

## Project status

Working MVP.

- [x] Canvas API integration (courses + assignments, paginated, idempotent)
- [x] Notes ingestion and chunking pipeline
- [x] Embedding + semantic retrieval, with reverse lookup
- [x] Match explainability and confidence calibration
- [x] Retrieval evaluation: labelled set, metrics, config sweep, LLM judge
- [x] Agent work sessions with citation traceability
- [x] Streamlit UI + FastAPI
- [ ] Lecture audio -> transcript via Whisper (pipeline accepts transcripts already)
- [ ] Google Classroom as a second integration
- [ ] Background/batched indexing for large corpora
- [ ] Multi-user accounts
- [ ] Export work-session output to Google Docs / Word

## License

MIT
