"""How the studying is actually going, and what to practise next.

All derived from `card_reviews`, which stores every grade ever given. Nothing
here calls a model or writes anything -- it is arithmetic over history, so it
costs nothing to ask and can be asked as often as the UI likes.

The one judgement worth defending is how "needs practice" is ranked. Raw
accuracy is the obvious choice and it is unstable where it matters most: every
card that has been seen once and missed sits at exactly 0%, indistinguishable
from a card missed ten times out of ten, and the list fills up with things the
student has barely met instead of the ones they keep failing.

So the rank shrinks each rate toward the middle by a few notional attempts.
A single miss lands near 0.4 rather than 0.0 -- still worth practising, but
below the card at 0/10, which is what a student would pick out themselves.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

#: An interval past which a card is not really being learned any more, it is
#: being maintained. Three weeks is the conventional line and it is arbitrary;
#: it is named here rather than buried so it can be argued with.
MASTERED_AFTER_DAYS = 21

#: Below this, a card is worth putting in front of the student again out of
#: schedule. Above it, the scheduler is handling things.
STRUGGLING_BELOW = 0.7

#: How many notional attempts the prior is worth. Four is a couple of
#: successes and a couple of failures: enough to stop one answer deciding a
#: card's fate, small enough that ten real attempts dominate it.
PRIOR_STRENGTH = 4
PRIOR_RATE = 0.5


def smoothed_rate(
    correct: int,
    attempts: int,
    strength: int = PRIOR_STRENGTH,
    prior: float = PRIOR_RATE,
) -> float:
    """Success rate, shrunk toward the middle when there is little evidence.

    Additive smoothing: pretend every card starts with `strength` attempts at
    `prior`, then add what actually happened. The effect is that

        0 of 1   -> 0.40    seen once, missed. Worth practising.
        0 of 10  -> 0.14    missed every time. Worth practising more.
        12 of 20 -> 0.58    genuinely shaky, and well attested.
        20 of 20 -> 0.92    never quite 1.0, because nothing is certain.

    The ordering is the point. Raw accuracy puts the first two at an identical
    0%, so a card met once outranks one failed ten times, and the practice
    list fills with noise.
    """
    if attempts <= 0:
        return prior
    return (correct + strength * prior) / (attempts + strength)


def state_of(card: dict) -> str:
    """"new" | "learning" | "mastered", the three a student cares about."""
    if not card.get("attempts"):
        return "new"
    if int(card.get("interval_days") or 0) >= MASTERED_AFTER_DAYS:
        return "mastered"
    return "learning"


def needs_practice(performance: list[dict], limit: int = 10) -> list[dict]:
    """Cards worth revisiting, worst first.

    Two exclusions, and the second is not obvious.

    A card never seen is not weak, it is new -- mixing the two buries the
    actual problems under everything the student has not started.

    A card never got *wrong* is not weak either, however thin its record. With
    a 0.5 prior a single correct answer scores 0.6, so without this rule
    everything answered once would sit in the practice list until it had been
    right three times running, which is most of the deck and therefore no
    guidance at all. Failure is the signal here; smoothing only orders the
    cards that have some.
    """
    scored = []
    for card in performance:
        attempts = int(card.get("attempts") or 0)
        correct = int(card.get("correct") or 0)
        if attempts == 0 or correct >= attempts:
            continue
        confidence = smoothed_rate(correct, attempts)
        if confidence >= STRUGGLING_BELOW:
            continue
        scored.append({
            **card,
            "confidence": round(confidence, 3),
            # Lapses break ties: two cards at the same rate, the one that has
            # been forgotten after being learned is the more fragile.
            "sort_key": (confidence, -int(card.get("lapses") or 0)),
        })
    scored.sort(key=lambda c: c["sort_key"])
    for card in scored:
        card.pop("sort_key", None)
    return scored[:limit]


def streak(activity: list[dict], today: Optional[date] = None) -> int:
    """Consecutive days studied, counting back from today.

    A day studied yesterday but not yet today still counts: the streak is not
    broken until a day passes with nothing in it, and telling someone at 9am
    that they have lost a fortnight's streak is both wrong and discouraging.
    """
    if not activity:
        return 0
    today = today or datetime.now(timezone.utc).date()
    days = {
        date.fromisoformat(entry["day"][:10])
        for entry in activity
        if entry.get("reviews")
    }
    if not days:
        return 0

    cursor = today if today in days else today - timedelta(days=1)
    if cursor not in days:
        return 0

    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def summarise(performance: list[dict], activity: list[dict]) -> dict:
    """The numbers a student reads before deciding what to do next."""
    states = {"new": 0, "learning": 0, "mastered": 0}
    attempts = correct = 0
    for card in performance:
        states[state_of(card)] += 1
        attempts += int(card.get("attempts") or 0)
        correct += int(card.get("correct") or 0)

    reviewed_recently = sum(int(day.get("reviews") or 0) for day in activity)
    return {
        "cards": len(performance),
        **states,
        "attempts": attempts,
        "correct": correct,
        "accuracy": round(correct / attempts, 3) if attempts else None,
        "streak_days": streak(activity),
        "reviews_recent": reviewed_recently,
        "activity": activity,
    }
