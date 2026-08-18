# The LLM in the product

There are two model-driven features. The work-session agent turns notes matched
to an assignment into a draft. The chat answers a question from whatever the
retriever finds. This is about the second, and about the constraints both work
under.

## The one rule

**The model answers from the student's notes or it says it cannot.**

Not "prefers to". A study tool that invents a plausible answer about material
the student never wrote down is worse than no tool: it is confidently wrong
about exactly the thing being revised, at the moment they are least able to
notice, and it sounds like their own notes while doing it.

Three layers enforce it, and they are layered because none is reliable alone.

**1. Nothing retrieved, no call.** If the retriever finds nothing scoring above
0.20, the model is never invoked. A call that does not happen cannot
hallucinate, and an empty corpus costs nothing.

The floor is not generous on purpose. The retrieval eval put a completely
unrelated note at 0.495 against a gradient-descent problem set, on the words
`effect`, `under` and `rate` — a respectable-looking number with no topical
content behind it. Anything that treats 0.5 as "relevant" is trusting that.

**2. The system prompt states the constraint** and makes refusing a success
rather than a failure. This is the weakest layer and is placed accordingly.

**3. The answer is checked afterwards.** Citations are resolved against the notes
actually supplied; any `[N<id>]` the model produced that was never given is
reported in `invented_note_ids` and the response is marked `grounded: false`.
This is a fact about the response, not a promise about the prompt.

A client that renders the text and ignores `grounded` is choosing to display
citations nobody checked. The flag exists so that is a decision.

## Measuring it

`python scripts/run_groundedness.py`

| metric | what it catches |
|---|---|
| `citation_validity` | invented ids — evidence that is not evidence |
| `citation_coverage` | answers with one citation and six paragraphs |
| `refusal_accuracy` | confident answers to questions the notes cannot support |
| `answer_rate` | a system that refuses everything to look safe |

The last two exist because the first two are trivially gamed. A system that
answers everything from three half-matched notes scores perfectly on validity
and coverage; `refusal_accuracy` is the number that catches it, and
`answer_rate` stops the opposite failure from scoring well.

Half the shipped cases are unanswerable, and two of those retrieve notes anyway
— "what did the lecturer say about transformer attention heads" matches on
lecture vocabulary, "what grade did I get on problem set 3" matches a real
assignment. Those are the cases worth having: layer one cannot save them.

## Notes are untrusted input

A student can paste anything into a note, and a lecture transcript contains
whatever the lecturer said. So retrieved text is treated as data:

- wrapped in `<note>` tags the system prompt explicitly names as data, not
  instructions
- a note containing its own closing tag has it neutralised, so it cannot escape
  the delimiter and have what follows read as instructions

**This is mitigation, not a guarantee.** Delimiting and instructing reduce the
success rate of prompt injection; they do not eliminate it, and anyone claiming
otherwise is selling something. What actually bounds the damage here is that the
model has no tools in the chat path — it reads notes and writes text. There is
nothing for an injected instruction to *do*: no sending, no deleting, no
spending beyond the one call already budgeted. Keep it that way, and any tool
added later needs this reasoning redone rather than inherited.

## Cost

Every call is recorded per user, and an allowance is checked before spending.

The cap is checked before the call and the cost recorded after, so a user can
overshoot by one message — a reply's size is not knowable until it exists.
Reserving an estimate and reconciling afterwards is a lot of machinery for a
bound that stays approximate either way, so the overshoot is accepted, bounded
by `max_tokens`, and stated rather than hidden.

The check sits after retrieval, so a refusal never costs anything or counts
against an allowance.

Unknown models are priced at the most expensive rate in the table: failing safe
here means failing expensive, so a newly released model counts against the
budget instead of slipping through it.

## Follow-up questions

"And what about alpha?" is clear to a human and retrieves nothing, because the
retriever sees the question and not the conversation. Left alone, a chat's
second turn is quietly worse than its first — and the failure presents as "your
notes do not cover this", which is the worst possible thing to tell someone
asking about the note they were just shown.

Short questions are widened with the last two user turns before searching. Only
the retrieval query changes; the model is asked exactly what the student typed.

Lexical rather than a model call to rewrite the query, deliberately. A rewrite
would be better and would also put a second round-trip in front of every
message, doubling latency on the turn the user is already waiting through.

## What this does not do

**Guarantee correctness.** Grounded means "traceable to a note you wrote", not
"true". If the note is wrong, the answer will be wrong and correctly cited.

**Defeat prompt injection.** See above.

**Persist conversations.** History is passed in by the client and not stored, so
there is no conversation to leak, and also no history across devices.

**Reason across the whole corpus.** Six notes reach the model. "Summarise
everything I wrote this term" is not this feature.

**Work without a key.** Retrieval, matching, the metrics, and the eval all run
with no credentials at all. Chat is the one part that needs `ANTHROPIC_API_KEY`,
and it fails with a clear 503 rather than degrading into something that looks
like an answer.
