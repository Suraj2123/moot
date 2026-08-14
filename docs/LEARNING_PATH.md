# Learning path: becoming an AI engineer through this codebase

The code in this repo is finished and working. That is a problem for learning: a
working system teaches you almost nothing, because the interesting part was the
decisions, and those are invisible once they've been made.

So this document is the other half. It turns the repo into a course. Each stage
names the decision that was made, tells you what to build or break yourself, and
gives you a way to check whether you actually understand it — not "does it run",
but "can you predict what happens before you run it".

**How to use this:** do the stages in order. Each has a *rebuild* task (write it
yourself, then diff against the shipped version) and a *stretch* task (extend it
past what's here). Don't read the reference file until you've attempted the
rebuild — reading a solution feels like learning and isn't.

Rough pacing: a stage is an evening or two. The whole path is 4–6 weeks at a few
hours a week. Stages 3 and 5 are the ones that matter most for employability.

---

## Stage 0 — Get it running and form opinions

Before writing anything:

```bash
pip install -r requirements.txt
python scripts/seed_demo.py
streamlit run app.py
```

Play with it for twenty minutes. Then write down, in a file, three things you
think are wrong with it. Keep that file. At the end of the path, revisit it —
some of your objections will have been right and some will turn out to be the
system working as intended. Knowing which is which is the skill.

**Check yourself:** open the "Retrieval quality" tab and run the sweep. Can you
explain, without looking at the code, why `precision@5` is capped at 0.4 for most
assignments? (Answer is in `studylink/evaluation/metrics.py`, but try first.)

---

## Stage 1 — Chunking: the boring decision that dominates everything

**The decision:** how do you cut a document into pieces small enough to embed,
without cutting through the middle of an idea?

Chunking is unglamorous and it moves your metrics more than your choice of
embedding model does. Most RAG systems that "don't work" have a chunking bug.

**Rebuild:** delete `studylink/chunking.py` and rewrite `chunk_text` from the
signature alone:

```python
def chunk_text(text: str, chunk_size: int = 180, chunk_overlap: int = 40) -> list[str]: ...
```

Make `tests/test_chunking.py` pass. Do not read the original first.

Things you will get wrong on the first attempt (everyone does):
- A paragraph longer than `chunk_size` — what happens? Infinite loop, or a chunk
  that blows the budget, or silently dropped text?
- `chunk_overlap >= chunk_size` — this is an infinite loop waiting to happen.
- Does your last chunk get emitted, or does it get lost when the loop exits?

**Stretch:** implement *semantic* chunking — split where the embedding similarity
between consecutive sentences drops below a threshold, instead of at a fixed word
count. Then run `python scripts/run_eval.py --sweep` and see whether it actually
beats the fixed-size version on this corpus. It may not. That result is worth
more than the implementation.

**Concept to internalise:** the unit you embed is the unit you retrieve. If your
chunk contains two unrelated ideas, its embedding is the average of two points in
vector space, which is a location that means neither of them.

Reference: `studylink/chunking.py`

---

## Stage 2 — Embeddings and vector search: build the primitive, then stop building it

**The decision:** what does "similar" mean, numerically, and where do the vectors
live?

**Rebuild:** implement cosine similarity search over a numpy matrix from scratch
— no libraries beyond numpy. Roughly 15 lines. Then answer, without running it:

- Why are all vectors L2-normalised on the way out of the provider? (Hint: what
  does a dot product between unit vectors equal?)
- The `hash` provider is pure lexical matching. Under what query would it beat a
  real neural embedding model? Under what query would it fail catastrophically
  where a neural model succeeds? Construct one of each and test them with
  `app.search_notes()`.

**Stretch:** get a Voyage API key (or `pip install sentence-transformers`), set
`EMBEDDING_PROVIDER`, re-run the eval, and write down the delta in `recall@5`
against the hash baseline. Then answer the question that separates engineers from
demo-builders: *is the improvement worth the latency and cost?* You now have the
numbers to answer it rather than guess.

**Concept to internalise:** exact search over 10k vectors is a millisecond and is
correct. Reach for FAISS/Chroma/pgvector when you have a measured latency problem,
not because a tutorial used one. Read the docstring at the top of
`studylink/vectorstore.py` for the version of this argument that ships here.

Reference: `studylink/embeddings.py`, `studylink/vectorstore.py`

---

## Stage 3 — Evaluation: the stage that makes you employable

Most people building with LLMs stop at "it looks good." The ability to say
"recall@5 went from 0.72 to 0.89 when I changed X, measured on 20 labelled pairs"
is the single most transferable skill in this whole project.

**The decision:** what does "correct retrieval" mean, and how do you measure it
without fooling yourself?

**Do this by hand first, before touching the code.** Open `data/eval/labels.json`.
Add five new labelled pairs of your own — and make at least two of them
*negative*: a note that looks relevant (same course, shared vocabulary) but
isn't. An eval set of only obvious positives cannot tell a good retriever from
one that returns everything.

Then:

```bash
python scripts/run_eval.py
```

**Rebuild:** implement `precision_at_k`, `recall_at_k`, `reciprocal_rank`, and
`ndcg_at_k` from their definitions. Make `tests/test_evaluation.py` pass. Then
answer these without running anything:

1. A retriever returns 5 notes; 2 are relevant; there were 3 relevant notes in
   total. What are P@5 and R@5?
2. You change the ranking so both relevant notes move from positions 4 and 5 to
   positions 1 and 2. Which of your four metrics change, and which don't?
3. Your system has recall@5 = 0.95 and MRR = 0.3. What is the user experience?
   (This is a real failure mode and the answer is why MRR is in the list.)

**Stretch — the honest-metrics problem:** `runner.py` reports *two* macro
averages, "unlabelled counted as irrelevant" and "judged pairs only". Read the
comment explaining why. Then construct a case where these two numbers tell
opposite stories, and decide which one you'd put in a README. This is the most
intellectually honest thing in the repo and the thing most projects get wrong.

**Stretch — LLM-as-judge:** with an `ANTHROPIC_API_KEY` set:

```bash
python scripts/run_eval.py --judge
```

This grades your labelled pairs with a model and reports Cohen's kappa against
your hand labels. Read the disagreements. Some will be the judge being wrong;
**some will be your labels being wrong**, and fixing those is real work. Only
once kappa is above ~0.6 is the judge trustworthy enough to label pairs you
haven't. Never quote a judge-derived number without reporting the agreement stat
that justifies it.

Reference: `studylink/evaluation/`

---

## Stage 4 — Explainability: making the system arguable

**The decision:** when the system says "this note is relevant", what evidence does
it show?

`explain()` in `studylink/retrieval.py` is deliberately dumb: shared terms and the
densest-overlap sentence. No second model call, nothing that can hallucinate.

**Rebuild:** design a *better* explanation and argue for it. Candidates:
- Attention-style: which chunk sentences contribute most to the similarity score?
- Contrastive: what does this note have that the note ranked just below it lacks?
- An LLM-generated one sentence explanation per match.

For each, ask: what does it cost per result, and can it lie? The third option is
the most impressive-looking and the only one that can produce a confident,
plausible, false explanation. That tradeoff is the lesson.

**Check yourself:** run the demo corpus and look at the matches for "Problem Set
3". The 4th result matches on the terms `effect`, `under`, `rate`, `reference` —
generic words with no topical content. The explanation surface caught a weak
match that the score alone made look respectable. Build the thing that lets you
notice that.

**Stretch:** the `calibrate_confidence` function blends the cosine score with
lexical overlap at a 75/25 weighting that I chose by judgement, not by fitting.
Fit it properly: use your labelled pairs to find the weighting that best
separates positives from negatives. Report whether it beat the guess.

Reference: `studylink/retrieval.py`

---

## Stage 5 — The agent: context engineering, not prompt engineering

**The decision:** what goes in the model's context window, in what order, and what
can the model go get for itself?

Look at `format_note_context()` in `studylink/agent.py`. It puts the *matched
chunk* first, then the surrounding note, truncated to a budget. Every part of that
is a decision:

- Why lead with the matched chunk rather than the note's opening?
- Why give the model the full note at all, when retrieval only scored one chunk?
- Why a per-note character budget instead of a global one?

**Rebuild:** write the system prompt yourself before reading `SYSTEM_PROMPT`. It
must produce output that (a) cites every claim, (b) refuses to fill gaps from the
model's own knowledge, and (c) says so when the retrieved notes look wrong. Then
test it: run a work session against an assignment whose notes you've deliberately
made irrelevant, and see whether the agent says "these notes don't cover this" or
cheerfully invents an outline. Most first-draft prompts do the second thing.

**The traceability check is the point.** `citation_coverage()` compares the note
ids the agent cited against the ids it was actually given. A non-empty
`hallucinated_ids` means the output is claiming provenance it doesn't have. This
is a cheap automated check on a class of failure that is otherwise invisible —
build the equivalent for whatever you work on next.

**Rebuild the loop:** the tool-use loop in `_run_loop` is about 30 lines. Write
it yourself from the API docs. The three things people get wrong:
1. Not appending the assistant's full `content` back (thinking and tool_use blocks
   must survive the round trip, unmodified).
2. Returning tool results in separate user messages instead of one — this
   silently trains the model out of parallel tool calls.
3. No iteration cap, so a confused model loops until you run out of money.

**Stretch:** give the agent a second tool — `get_assignment_details(assignment_id)`
— so it can compare the current assignment against related ones. Then measure
whether output quality improved, using a rubric you write. If you can't measure
it, you've built a feature you can't defend.

Reference: `studylink/agent.py`

---

## Stage 6 — Making it real

The three things that separate this from a portfolio piece:

**Ingestion at scale.** Right now `add_note` reindexes synchronously. Add 500
notes and the UI blocks. Fix it: batch the embedding calls, index in the
background, show progress. Measure the before and after.

**Lecture transcripts (a stretch goal in the spec).** The pipeline already
accepts `source_type="transcript"`. Add Whisper transcription of an audio file,
feed it through the same chunker, and then check whether your retrieval metrics
*hold up* on transcript text — spoken language has different statistics from
written notes, and chunk sizes tuned on notes are often wrong for transcripts.
Re-run the sweep with transcripts in the corpus and see.

**Multi-user.** Every query in `store.py` would need a user scope. Sketch what
breaks — it's more than adding a `user_id` column. The vector store's `matrix()`
loads *every* vector; with multiple users that is both slow and a data-leak
waiting to happen.

---

## What to read alongside

- Anthropic's docs on [tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)
  and [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
  — caching is the difference between a demo and something you can afford to run.
- *Introduction to Information Retrieval* (Manning, Raghavan, Schütze), chapter 8
  — free online, and the metrics chapter is the canonical treatment of everything
  in Stage 3.
- Read one production RAG post-mortem. Search for teams writing about what broke.
  The failure modes are always chunking, evaluation, or context, in that order.

---

## The thing worth remembering

The RAG pipeline in this repo is maybe 400 lines. The evaluation harness is a
similar size. That ratio is not an accident, and it is the actual lesson: for
anything built on a model, the system that tells you whether it works is
comparable in size and importance to the system that does the work.

Most people skip it because it isn't fun, then can't tell whether their changes
help. Being the person who can produce the number is the job.
