# Sharing decks

A deck is built out of a student's notes, so publishing one is the only action
in moot that moves their material to somebody else. Everything below is shaped
by that.

## Three states

| Visibility | Reachable by link | In Discover | Forkable |
|---|---|---|---|
| `private` (default) | no | no | no |
| `unlisted` | yes | **no** | yes |
| `public` | yes | yes | yes |

`unlisted` and `public` are genuinely different permissions, not a strength
dial. "Anyone with the link" is a capability someone hands out deliberately;
"anyone who searches" is not. Surfacing an unlisted deck in Discover would
convert the first into the second silently, so `PublicDeckIndex` filters on
`visibility == 'public'` and nothing else can reach it.

## What leaves the account

A shared card carries exactly three fields: `id`, `front`, `back`.

Not `evidence`. That field quotes the source note verbatim — for a generated
card, a whole sentence of it — which makes it the field most likely to drag
surrounding private material onto a public page. Not the scheduling columns
either: when the owner last saw a card is a fact about the owner.

The owner is named by `display_name`, or "A moot user" when they never set one.
Never by email — that is a contact detail they gave us to log in with.

`tests/test_isolation.py` asserts the key set of a shared card rather than
searching the payload for a canary, so a column added later cannot quietly join
the response.

## Slugs

`decks.slug` is ten random characters, not the primary key. A sequential id in
a URL tells anyone who looks how many decks the service holds and lets them
walk the range to find every public one, which is a different thing from
"public".

The alphabet has no `0`/`O`, no `1`/`l`/`I`, and no vowels. The first two
because share links get read aloud and typed back in; the third because a
random string that spells something unfortunate is not what anyone wants to
paste into a class group chat. Ten characters is ~47 bits; collisions are
handled by retrying against the unique index rather than by trusting that
arithmetic.

Every deck gets a slug at creation, not at the moment of sharing. Sharing is a
switch someone flips expecting a link immediately.

## Forking

`POST /d/{slug}/fork` copies the deck and its cards into the caller's account
with **fresh scheduling state and no review history**. A schedule is a claim
about one person's memory, and telling a forker they already know things they
have never seen is worse than making them start. `forked_from_id` records the
origin and is `SET NULL`, so deleting the original does not delete the copy.

A fork is always private. Copying a public deck must not publish the copy.

## Withdrawing

Making a deck private deletes its search vector in the same request, not in a
job. A student who un-publishes has withdrawn consent, and "it stops being
findable within a minute" is not what withdrawing consent means.

## Search

Deck discovery reuses the embedding index that matches notes to assignments —
`embeddings` is keyed by `(owner_type, owner_id)`, so `deck` is one more owner
type rather than a second index. Semantic rather than keyword because someone
looking for a deck rarely knows what its author called it, and because the
machinery was already there.

A deck's search text is its title, fronts, and backs. The default provider is a
bag-of-words hash, so what it has to work with is vocabulary, and half a deck's
vocabulary lives in its answers.

The retriever's score threshold applies. A cosine search always returns its top
k however badly they match, so without a floor, searching "photosynthesis"
against a service holding one deck about Roman history returns that deck and
the page has told a lie about what it found.

## What this does not do

- **No moderation.** Nothing scans a published deck. A deployment expecting
  strangers to publish to each other needs a story for that, and this does not
  have one.
- **No unpublish notification.** Someone who forked a deck keeps their copy
  when the original goes private. That is deliberate — the copy is theirs — but
  it does mean unpublishing does not retract what has already been taken.
- **No edit propagation.** A fork is a snapshot. Improvements to the original
  never reach copies, and copies never touch the original.
- **No attribution beyond `forked_from_id`.** The copy does not display where
  it came from.
