# Matching notes to what you are studying for

The question this feature answers is "which of my notes bear on this?", where
*this* is any piece of text: an exam topic, a syllabus section, a question you
have been set, a pasted assignment brief.

## Why it needed fixing

For most of this project's life that question could only be asked about a
Canvas assignment. `upsert_assignment` had no caller outside `canvas.py`,
`assignments.course_id` was `NOT NULL`, and a course only ever came from a
sync. So the retriever, the evidence layer, the confidence calibration and the
evaluation harness — the most carefully built part of the codebase — did
nothing at all for a student whose university disables personal access tokens.
Which is many of them.

The fix was small and structural: make `course_id` nullable, add a `source`
column, and route to the write path that already existed.

## Targets and assignments are the same row

There is one table, `assignments`, and two kinds of row in it:

| `source` | Written by | Editable in moot | Has a course |
|---|---|---|---|
| `canvas` | the syncer | no | yes |
| `manual` | the student | yes | no |

They share a table because they are the same question and the same machinery.
The retriever embeds `retrieval_text` and ranks note chunks against it; nothing
downstream cares where the text came from.

A Canvas row is refused for edit and delete with a 409. It is a copy of
something owned elsewhere, so a local edit would survive until the next sync
and then vanish — the same trap as editing a flashcard that a note writes, and
refused for the same reason.

The table keeps its name. Renaming it to `study_targets` is the tidier concept
and would also rewrite `eval_labels`, the evaluation harness, the retriever's
`owner_type`, and every test that mentions an assignment — a large diff whose
only product is vocabulary. The vocabulary that matters is the one in the UI,
and that says "Match".

## What happens when you create one

1. Any small backlog of unindexed notes is indexed inline (see below).
2. The target row is written with `source='manual'` and no course.
3. Its text is embedded immediately and stored under `owner_type='assignment'`.
4. Matches are computed and returned **in the same response**.

Step 4 matters. Typing a topic and seeing which notes cover it is one action
from the student's side; a version that saves a row and makes them click again
reads as a form rather than an answer.

## The indexing compromise

A note is chunked and embedded by the background worker, not on save. So a
student who writes three notes and immediately names a target would match
nothing — and "no matches" is indistinguishable from "this feature is broken".

`catch_up_index` indexes the backlog inline when it is 25 notes or fewer, and
leaves it to the worker otherwise. The threshold is a judgement, not a
measurement: someone with a handful of notes is exactly the person this feature
has to work for immediately, and someone who has just uploaded two hundred PDFs
has a worker for that and should not discover the difference as a
thirty-second POST. The response carries `notes_pending` so the UI can say
"still indexing" rather than implying nothing matched.

## What this does not do

- **No note-to-note linking.** Related notes do not surface each other. That is
  a different feature built on the same embeddings, and it is not built.
- **No re-matching on note change.** A target's matches are computed when asked
  for, so they are current — but nothing notifies you that a new note now
  matches an old target.
- **No ranking across targets.** "Which of my exams am I least prepared for" is
  answerable from this data and is not implemented.
- **No LLM anywhere.** Matching is embeddings and lexical evidence. The
  confidence number is a calibrated cosine score, not a judgement, and
  `docs/LLM.md` does not apply here.
