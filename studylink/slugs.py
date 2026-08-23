"""Short public identifiers for things that get shared by link.

A deck's URL cannot be its primary key. A sequential id tells anyone who looks
how many decks the whole service holds, and it lets them walk the range to find
every public one -- which is a different thing from "public", because a deck
being readable to someone who has the link is not the same as being indexed by
a stranger with a for-loop.

So: random, from an alphabet chosen to survive being read aloud and typed back.
No `0`/`O`, no `1`/`l`/`I`, and no vowels at all, which is the cheap way to
avoid a generated slug spelling something unfortunate on a page the owner is
about to send to a class group chat.

Ten characters of this alphabet is about 47 bits. At a million decks the odds
of any collision existing at all are around one in 300 -- and collisions are
still handled by retrying against the unique index rather than by trusting that
arithmetic, because "unlikely" is not a constraint the database enforces.
"""

from __future__ import annotations

import secrets

# 31 characters: digits and consonants, minus the ones that look like each
# other in the fonts people actually read URLs in.
ALPHABET = "23456789bcdfghjkmnpqrstvwxyzBCDFGHJKMNPQRSTVWXYZ"
LENGTH = 10


def new_slug(length: int = LENGTH) -> str:
    """A fresh random slug. Uses `secrets`, not `random`.

    Not because a slug is a secret -- an unlisted deck's URL is closer to a
    capability than a name, and `random` is seeded predictably enough that
    someone who saw a few slugs could guess the next ones.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
