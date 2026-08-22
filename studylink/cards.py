"""Flashcards and practice tests, generated from the student's own notes.

Three separable things live here, and only one of them costs money.

**Generation** asks the model to turn a note into question/answer pairs. It is
the only part that calls an LLM, and it is constrained the same way everything
else in this product is: each card must quote the sentence from the note that
supports it, and a card whose quote is not actually in the note is dropped
rather than shown. A flashcard that teaches something the notes never said is
worse than no flashcard, because the student will study it and believe it.

**Scheduling** decides when a card comes back. SM-2 lite -- see `schedule`.
No LLM, no network, entirely deterministic.

**Test assembly** builds a practice test out of cards that already exist. Also
deterministic: the wrong answers are other cards' answers from the same deck,
which costs nothing and produces better distractors than a model does, because
they are drawn from the same material and are therefore actually confusable.

What this does not do:

  * Cloze deletions, image occlusion, or audio. Text pairs only.
  * Any claim that the scheduler is optimal. It is a defensible default, and
    every review is stored so a better one can be fitted later.
  * Grading written answers. The written mode compares normalised strings and
    says "close" rather than pretending to understand a paraphrase.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import AGENT_MODEL

logger = logging.getLogger(__name__)

# Grades, lowest to highest. Deliberately four, not Anki's six: more options
# than a student can distinguish under time pressure just adds noise to the
# signal the scheduler runs on.
FORGOT, HARD, GOOD, EASY = 0, 1, 2, 3
GRADES = {FORGOT, HARD, GOOD, EASY}

# Ease is stored in hundredths so the column can stay an integer: 250 is the
# conventional 2.5 starting factor, 130 the 1.3 floor. Floats in a column
# that accumulates multiplicatively drift, and this one is read and written
# on every review.
STARTING_EASE = 250
MINIMUM_EASE = 130

MAX_CARDS_PER_NOTE = 20
MIN_NOTE_CHARS = 120


class CardError(ValueError):
    """Something stops this note becoming cards. Safe to show a user."""


class GenerationUnavailable(RuntimeError):
    """No model configured. Same shape as the chat's unavailability."""


@dataclass
class DraftCard:
    """A generated pair before it is stored, plus where it came from."""

    front: str
    back: str
    evidence: str = ""
    note_id: Optional[int] = None


@dataclass
class TestQuestion:
    card_id: int
    prompt: str
    #: Present for multiple choice; empty for written answers.
    choices: list[str] = field(default_factory=list)
    answer: str = ""
    kind: str = "multiple_choice"

    def as_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "prompt": self.prompt,
            "choices": self.choices,
            "answer": self.answer,
            "kind": self.kind,
        }


# --------------------------------------------------------------- scheduling


def schedule(
    grade: int,
    interval_days: int,
    ease: int,
    reviews: int,
    now: Optional[datetime] = None,
) -> tuple[int, int, datetime]:
    """SM-2 lite. Returns (interval_days, ease, due_at).

    The rules, in full, because a scheduler nobody can predict is one nobody
    trusts:

      * Forgetting resets the interval to one day and takes 20 points off ease.
        The card comes back tomorrow, not in ten minutes -- same-day repeats
        mostly measure short-term memory, which is not the thing being trained.
      * The first two successful reviews are fixed at 1 and 6 days. Multiplying
        from zero would send a card three weeks out on its second sighting.
      * After that the interval multiplies by ease, adjusted by how it felt.
      * Ease has a floor. Without one, a card the student keeps failing decays
        toward a multiplier of ~1 and is shown forever at the same interval,
        which feels like punishment and teaches nothing.

    Deliberately not: fuzz, learning steps, leech suspension, or per-deck
    configuration. Each is a real improvement and each needs evidence from
    actual review data, which `card_reviews` is accumulating.
    """
    if grade not in GRADES:
        raise CardError(f"grade must be one of {sorted(GRADES)}")
    now = now or datetime.now(timezone.utc)

    if grade == FORGOT:
        return 1, max(MINIMUM_EASE, ease - 20), now + timedelta(days=1)

    adjustment = {HARD: -15, GOOD: 0, EASY: 15}[grade]
    ease = max(MINIMUM_EASE, ease + adjustment)

    if reviews == 0:
        interval = 1
    elif reviews == 1:
        interval = 6
    else:
        multiplier = ease / 100 * {HARD: 0.6, GOOD: 1.0, EASY: 1.3}[grade]
        interval = max(1, round(interval_days * multiplier))

    return interval, ease, now + timedelta(days=interval)


# ----------------------------------------------------------- test assembly


def build_test(
    cards: list[dict],
    kind: str = "multiple_choice",
    length: int = 10,
    rng: Optional[random.Random] = None,
) -> list[TestQuestion]:
    """Assemble a practice test from cards that already exist.

    No model call. Distractors are other answers from the same deck, which is
    both free and better: wrong options drawn from the same lecture are
    genuinely confusable, where invented ones are usually obviously wrong and
    turn the test into a reading-comprehension exercise.

    Falls back to written answers when a deck is too small to offer choices --
    three cards cannot produce a four-option question, and padding with
    nonsense would make the test easier, not harder.
    """
    if not cards:
        raise CardError("This deck has no cards yet.")
    rng = rng or random.Random()
    pool = list(cards)
    rng.shuffle(pool)
    selected = pool[: max(1, length)]

    if kind == "written" or len(cards) < 4:
        return [
            TestQuestion(
                card_id=card["id"], prompt=card["front"],
                answer=card["back"], kind="written",
            )
            for card in selected
        ]

    questions = []
    for card in selected:
        others = [c["back"] for c in cards if c["id"] != card["id"] and c["back"] != card["back"]]
        distractors = rng.sample(others, k=min(3, len(others)))
        choices = [card["back"], *distractors]
        rng.shuffle(choices)
        questions.append(
            TestQuestion(
                card_id=card["id"], prompt=card["front"],
                choices=choices, answer=card["back"], kind="multiple_choice",
            )
        )
    return questions


def normalise_answer(text: str) -> str:
    """For comparing a typed answer to the stored one.

    Punctuation, case, articles, and spacing are noise here -- someone who
    wrote "the mitochondrion" for "Mitochondrion" knows the answer, and marking
    them wrong teaches them to distrust the tool rather than to study.
    """
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def grade_written(given: str, expected: str) -> str:
    """"correct" | "close" | "wrong", and no pretence of understanding.

    "close" exists because exact-match grading on free text is brutal and
    fully-automatic paraphrase grading needs a model call per answer. Telling
    the student it was close and showing them the real answer lets them make
    the call, which is honest about what this can actually determine.
    """
    given_n, expected_n = normalise_answer(given), normalise_answer(expected)
    if not given_n:
        return "wrong"
    if given_n == expected_n:
        return "correct"
    given_words, expected_words = set(given_n.split()), set(expected_n.split())
    if not expected_words:
        return "wrong"
    overlap = len(given_words & expected_words) / len(expected_words)
    return "close" if overlap >= 0.6 else "wrong"


# ---------------------------------------------------------------- generation


SYSTEM_PROMPT = """You write flashcards from a student's own lecture notes.

Rules:
- Every card must be answerable from the notes alone. Never add outside
  knowledge, even when it is correct.
- `evidence` must be an exact substring of the note, copied character for
  character, that supports the answer. Do not paraphrase it.
- Fronts are questions or prompts, not headings. "What does the learning rate
  control?" not "Learning rate".
- Backs are short: a phrase or a sentence. If an answer needs a paragraph, the
  card is testing too much at once -- split it.
- Prefer the things a student is actually examined on: definitions,
  distinctions, causes, conditions, formulas and what their terms mean.
- Skip administrative content: due dates, room numbers, reading lists.

Return JSON only, of the form:
{"cards": [{"front": "...", "back": "...", "evidence": "..."}]}
"""


def build_prompt(title: str, body: str, count: int) -> str:
    return (
        f"Write up to {count} flashcards from this note.\n\n"
        f"Title: {title}\n\n"
        f"<note>\n{body}\n</note>"
    )


def parse_cards(text: str) -> list[DraftCard]:
    """Pull cards out of the model's reply, tolerating the usual wrappers.

    Models fence JSON in markdown often enough that failing on it would be
    choosing to be brittle about something trivially fixable.
    """
    if not (text or "").strip():
        return []
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("card generation returned unparseable JSON")
        return []

    drafts = []
    for item in payload.get("cards", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        front = str(item.get("front", "")).strip()
        back = str(item.get("back", "")).strip()
        if not front or not back:
            continue
        drafts.append(
            DraftCard(front=front, back=back, evidence=str(item.get("evidence", "")).strip())
        )
    return drafts


def _norm_for_evidence(text: str) -> str:
    """Whitespace-insensitive comparison for the evidence check.

    Extraction from a PDF re-wraps lines, so an otherwise perfect quote can
    differ from the note by a newline. Rejecting on that would drop good cards
    for a formatting difference.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def verify(drafts: list[DraftCard], body: str) -> tuple[list[DraftCard], list[DraftCard]]:
    """Split drafts into (grounded, rejected) by whether the quote is real.

    This is the check that makes generated cards trustworthy. A model asked for
    an exact quote usually gives one; when it does not, that card is precisely
    the one that invented something. Returning the rejects rather than
    discarding them silently means the caller can say how many were dropped,
    which is a measurable groundedness signal rather than an assurance.
    """
    haystack = _norm_for_evidence(body)
    grounded, rejected = [], []
    for draft in drafts:
        evidence = _norm_for_evidence(draft.evidence)
        if evidence and evidence in haystack:
            grounded.append(draft)
        else:
            rejected.append(draft)
    return grounded, rejected


@dataclass
class Generated:
    cards: list[DraftCard]
    rejected: int = 0
    usage: dict = field(default_factory=dict)


class CardWriter:
    """Turns one note into candidate cards. The only part of this file that
    spends money."""

    def __init__(self, client=None, model: str = AGENT_MODEL, max_tokens: int = 2000):
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise GenerationUnavailable(
                    "The `anthropic` package is not installed."
                ) from exc
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:
                raise GenerationUnavailable(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY."
                ) from exc
        return self._client

    def write(self, title: str, body: str, count: int = 10) -> Generated:
        if len((body or "").strip()) < MIN_NOTE_CHARS:
            raise CardError(
                "That note is too short to make cards from. Add more detail, or "
                "write the cards yourself."
            )
        count = max(1, min(count, MAX_CARDS_PER_NOTE))

        message = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(title, body, count)}],
        )
        text = "".join(
            getattr(block, "text", "") for block in getattr(message, "content", [])
        )
        grounded, rejected = verify(parse_cards(text), body)

        raw = getattr(message, "usage", None)
        usage = {
            "input_tokens": getattr(raw, "input_tokens", 0) or 0,
            "output_tokens": getattr(raw, "output_tokens", 0) or 0,
        } if raw else {}

        return Generated(cards=grounded[:count], rejected=len(rejected), usage=usage)
