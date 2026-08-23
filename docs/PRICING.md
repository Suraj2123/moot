# What costs money

Almost nothing, and that is a design constraint rather than a launch promise.

Every feature a student uses daily runs with no API key and no network call to
anyone: writing cards in a note, reviewing them, practice tests, progress,
publishing a deck, searching public decks, copying one. If a feature needs a
model, it needs a deterministic fallback or it does not ship on the free path.

## The free path

| Feature | What makes it free |
|---|---|
| `term :: definition` cards | `outline.py` is a parser. No model is involved at any point |
| Scheduling and review | `cards.schedule` is arithmetic — SM-2 lite, ~40 lines |
| Practice tests | Distractors are other answers from the same deck, so there is nothing to generate |
| Progress and weak cards | `progress.py` is arithmetic over the review log |
| Note upload (PDF, docx, md, txt) | `documents.py` parses locally. No OCR, so no vision model |
| Note search and assignment matching | The default embedding provider is a hashing bag-of-words that runs in-process |
| Publishing, discovering, forking decks | Deck search reuses that same offline embedding index |

The embedding provider matters here. `EMBEDDING_PROVIDER=hash` is the default
and it is genuinely local — it hashes tokens into a fixed-dimension vector.
It is measurably worse than a real embedding model on the retrieval eval, and
it is what keeps the whole retrieval half of the product free. `voyage` and
`sentence-transformers` are available for anyone who wants better numbers;
neither is required.

## The paid path

Three features call Anthropic:

- **Generating cards from prose** — a note you did not mark up with `::`.
- **Asking questions of your notes** — the grounded chat.
- **Work sessions** — the agent that drafts from an assignment plus its notes.

Every one has a free alternative that does the same job less automatically:
write the cards yourself with `::`, search your notes instead of asking about
them, read the matched notes instead of having them synthesised.

## Who pays

Three arrangements, in order of preference:

1. **Nobody.** The deployment has no `ANTHROPIC_API_KEY` at all. The paid
   features return 503 with an explanation; everything else works. This is a
   legitimate way to run moot and the tests all pass in it.

2. **The user.** They store their own key in Settings. It is encrypted through
   `vault.py` exactly as a Canvas token is, bound to their account, and there
   is no read path that returns it. Their spend is recorded in `llm_usage` but
   the monthly allowance does not apply — that allowance exists to bound what
   the *operator* pays for, and the operator is not paying.

3. **The operator.** `ANTHROPIC_API_KEY` is set and users without their own key
   spend against `LLM_MONTHLY_BUDGET_USD` (default $2.00 per user per rolling
   30 days). The cap is checked before each call and the cost recorded after,
   so a user can overshoot by at most one message.

Storing a user key requires `STUDYLINK_SECRET_KEY`. Without it the Settings
card says so plainly rather than failing at save time.

## What this document does not claim

- **That the free path is as good.** A hashing embedder retrieves worse than a
  trained one, and cards you write yourself take longer than cards a model
  writes. The claim is that the free path is complete, not that it is equal.
- **That hosting is free.** Someone pays for the container and the database.
  This is about per-user marginal cost, which is what decides whether a free
  tier survives contact with growth.
- **That a user key is unlimited.** Anthropic's own rate limits and balance
  still apply, and when they bite, the error says so — see `llm.py`, which
  turns a provider failure into a sentence naming what to do about it.
