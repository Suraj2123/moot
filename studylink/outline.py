"""Notes as an outline, with flashcards written inline.

The idea this borrows is that authoring cards separately from notes is the step
nobody keeps doing. So the note *is* the card deck: a line written

    learning rate :: controls the size of each gradient step

is a note about the learning rate and a flashcard, and neither had to be
created twice. Nothing here calls a model, which means cards cost nothing and
work offline -- the generated ones in `cards.py` are for turning prose you
already wrote into practice, not the primary path.

Syntax, all of it:

    Front :: Back          one card, front to back
    Front ::: Back         two cards, both directions
    Front >> Back          same as ::, for people who prefer an arrow
    ... {{cloze}} ...      one card per cloze, the rest of the line as context

Indentation nests, and a nested card inherits its parents as context, so

    Photosynthesis
      light reactions :: split water, release oxygen

asks "Photosynthesis > light reactions" rather than a bare "light reactions"
that could belong to any lecture in the semester.

The hard part is not parsing, it is identity across edits. A student who fixes
a typo in an answer must not lose three weeks of review history for that card,
and one who rewrites a question has genuinely made a different card. So cards
are keyed on their *front* text: change the answer and the schedule survives,
change the question and it is a new card. That is a judgement call, and it is
the one that makes editing notes safe.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

# Two spaces or one tab per level. Four-space indents therefore read as two
# levels, which is what someone pasting from an editor expects.
INDENT = 2

BIDIRECTIONAL = ":::"
FORWARD = "::"
ARROW = ">>"

CLOZE = re.compile(r"\{\{(.+?)\}\}")
BULLET = re.compile(r"^[-*+]\s+")

#: How much of a line may be a separator before we stop believing it is one.
#: "a :: b :: c" is ambiguous and splitting on the first is the least
#: surprising reading.
MAX_SIDE_CHARS = 500


@dataclass
class Line:
    """One line of the outline, with its nesting depth."""

    depth: int
    text: str
    number: int


@dataclass
class OutlineCard:
    front: str
    back: str
    #: The line exactly as written, so a card can always be traced to the note.
    evidence: str
    #: Ancestor bullets, outermost first. Rendered into the front as context.
    path: list[str] = field(default_factory=list)
    kind: str = "pair"  # pair | reverse | cloze

    @property
    def asked_front(self) -> str:
        """The front as the student is actually asked it, ancestry included.

        Not applied to cloze: the sentence already carries its context, and
        prefixing it would ask the student to read the same fact twice.
        """
        if self.kind.startswith("cloze") or not self.path:
            return self.front
        return " \u203a ".join([*self.path, self.front])

    @property
    def source_key(self) -> str:
        """Stable identity across edits. See the module docstring.

        Keyed on the question *as asked*, which is why the ancestry is in it:
        two lines reading `cell :: ...` under Biology and under Chemistry are
        different cards, and keying on the bare front would collide them into
        one -- silently dropping whichever the sync saw second.

        Hashed because the front can be long and this is an indexed lookup,
        and normalised so whitespace and capitalisation changes do not orphan
        a card's history.
        """
        basis = re.sub(r"\s+", " ", f"{self.kind}|{self.asked_front}").strip().lower()
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> dict:
        return {
            "front": self.front,
            "back": self.back,
            "evidence": self.evidence,
            "kind": self.kind,
            "source_key": self.source_key,
        }


def depth_of(raw: str) -> int:
    """Indent depth, counting a tab as one level."""
    depth = 0
    for char in raw:
        if char == "\t":
            depth += 1
        elif char == " ":
            depth += 1 / INDENT
        else:
            break
    return int(depth)


def read_lines(text: str) -> Iterator[Line]:
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        depth = depth_of(raw)
        stripped = BULLET.sub("", raw.strip())
        if stripped:
            yield Line(depth=depth, text=stripped, number=number)


def split_pair(text: str) -> Optional[tuple[str, str, str]]:
    """(front, back, kind) if this line declares a card, else None.

    `:::` is checked before `::` because the shorter one is a prefix of it and
    matching first would silently turn every bidirectional card into a forward
    one with a colon stuck to the answer.
    """
    for token, kind in ((BIDIRECTIONAL, "reverse"), (FORWARD, "pair"), (ARROW, "pair")):
        if token not in text:
            continue
        front, _, back = text.partition(token)
        front, back = front.strip(), back.strip()
        if front and back and len(front) <= MAX_SIDE_CHARS and len(back) <= MAX_SIDE_CHARS:
            return front, back, kind
    return None


def cloze_cards(text: str, path: list[str]) -> list[OutlineCard]:
    """One card per deletion, each hiding only its own span.

    Hiding every deletion at once would make a line with four of them a single
    card testing four facts, which is the thing spaced repetition is
    specifically bad at.
    """
    spans = list(CLOZE.finditer(text))
    if not spans:
        return []

    cards = []
    for target in spans:
        rendered = []
        cursor = 0
        for span in spans:
            rendered.append(text[cursor : span.start()])
            # Other deletions on the line stay visible: they are the context
            # that makes this one answerable.
            rendered.append("____" if span is target else span.group(1))
            cursor = span.end()
        rendered.append(text[cursor:])
        cards.append(
            OutlineCard(
                front="".join(rendered).strip(),
                back=target.group(1).strip(),
                evidence=CLOZE.sub(r"\1", text).strip(),
                path=list(path),
                kind="cloze",
            )
        )
    return cards


def parse(text: str) -> list[OutlineCard]:
    """Every card declared in this note, in the order they appear."""
    cards: list[OutlineCard] = []
    ancestors: list[str] = []

    for line in read_lines(text):
        # Trim the ancestor stack to this line's depth before using it, so a
        # dedent drops the branch we just left.
        del ancestors[line.depth :]

        pair = split_pair(line.text)
        if pair:
            front, back, kind = pair
            cards.append(
                OutlineCard(
                    front=front, back=back, evidence=line.text,
                    path=list(ancestors), kind=kind,
                )
            )
            # A card's front can still be a parent -- notes nest under the
            # concept they elaborate.
            ancestors.append(front)
            continue

        found = cloze_cards(line.text, ancestors)
        if found:
            cards.extend(found)
            continue

        ancestors.append(line.text)

    return cards


def contextual_front(card: OutlineCard) -> str:
    """The front as it should be asked, with its ancestry.

    Only for `pair` and `reverse`: a cloze line already carries its own
    context in the surrounding sentence, and prefixing it would ask the
    student to read the same fact twice.
    """
    if card.kind == "cloze" or not card.path:
        return card.front
    return " › ".join([*card.path, card.front])


def to_cards(text: str) -> list[dict]:
    """Parsed cards in the shape the store writes, reverses expanded.

    A `:::` line becomes two rows rather than one row with a flag, because the
    two directions are genuinely different cards: recognising a term and
    producing it are separate skills, and they should be scheduled separately.
    """
    rows = []
    for card in parse(text):
        front = card.asked_front
        rows.append({
            "front": front,
            "back": card.back,
            "evidence": card.evidence,
            "kind": card.kind,
            "source_key": card.source_key,
        })
        if card.kind == "reverse":
            # The flipped card is asked without ancestry: its front is the
            # definition, which stands on its own.
            flipped = OutlineCard(
                front=card.back, back=front, evidence=card.evidence,
                path=[], kind="reverse-back",
            )
            rows.append({
                "front": card.back,
                "back": front,
                "evidence": card.evidence,
                "kind": "reverse-back",
                "source_key": flipped.source_key,
            })
    return rows


def count(text: str) -> int:
    """How many cards this note currently declares. Used by the editor."""
    return len(to_cards(text))
