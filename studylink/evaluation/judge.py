"""LLM-as-judge relevance scoring.

Hand labels are the ground truth; the judge is for scaling past what you are
willing to label by hand. Used correctly it is a second annotator, not a
replacement for the first one.

The workflow this supports:

  1. Hand-label ~20 pairs. That is the trusted set.
  2. Run `agreement()` to check the judge against those hand labels.
  3. If agreement is high (Cohen's kappa above ~0.6), use `judge_pairs()` to
     label the long tail. If it is low, fix the judge prompt before trusting a
     single number it produces.

Skipping step 2 is the classic mistake: an unvalidated judge produces confident
numbers that measure the judge's taste rather than the retriever's quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from ..config import JUDGE_MODEL
from ..models import Assignment, Note

JUDGE_SYSTEM = """You are grading whether a student's note is relevant to a course \
assignment, for a retrieval system's evaluation set.

"Relevant" means: a student working on this assignment would be meaningfully helped by \
seeing this note. Shared course, shared jargon, or shared topic area are NOT enough on \
their own -- the note must bear on what the assignment actually asks the student to do.

Grade on this scale:
  0  irrelevant: no bearing on the assignment
  1  marginal: same subject area, but does not help with this specific task
  2  relevant: covers material the assignment needs
  3  essential: directly covers a core requirement of the assignment

Be willing to give 0 and 1. A judge that grades everything 2-3 provides no signal."""

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "grade": {"type": "integer", "enum": [0, 1, 2, 3]},
        "reason": {
            "type": "string",
            "description": "One sentence, citing what in the note does or does not bear on the assignment.",
        },
    },
    "required": ["grade", "reason"],
    "additionalProperties": False,
}


@dataclass
class Judgement:
    assignment_id: int
    note_id: int
    grade: int
    reason: str

    @property
    def relevant(self) -> bool:
        """Binarise for comparison with hand labels. 2+ counts as relevant."""
        return self.grade >= 2


class RelevanceJudge:
    def __init__(self, client=None, model: str = JUDGE_MODEL, note_chars: int = 3000) -> None:
        self._client = client
        self.model = model
        self.note_chars = note_chars

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _prompt(self, assignment: Assignment, note: Note) -> str:
        body = note.body.strip()
        if len(body) > self.note_chars:
            body = body[: self.note_chars].rsplit(" ", 1)[0] + " [...truncated]"
        return (
            f"ASSIGNMENT: {assignment.name}\n"
            f"COURSE: {assignment.course_name}\n"
            f"DESCRIPTION:\n{assignment.description or '(none)'}\n\n"
            f"NOTE TITLE: {note.title}\n"
            f"NOTE COURSE: {note.course_name or 'unassigned'}\n"
            f"NOTE BODY:\n{body}"
        )

    def judge(self, assignment: Assignment, note: Note) -> Judgement:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=JUDGE_SYSTEM,
            output_config={
                "format": {"type": "json_schema", "schema": RELEVANCE_SCHEMA},
                "effort": "low",
            },
            messages=[{"role": "user", "content": self._prompt(assignment, note)}],
        )
        if response.stop_reason == "refusal":
            return Judgement(assignment.id, note.id, 0, "judge declined to grade this pair")

        text = next((b.text for b in response.content if b.type == "text"), "{}")
        payload = json.loads(text)
        return Judgement(
            assignment_id=assignment.id,
            note_id=note.id,
            grade=int(payload.get("grade", 0)),
            reason=str(payload.get("reason", "")),
        )

    def judge_pairs(self, pairs: Sequence[tuple[Assignment, Note]]) -> list[Judgement]:
        return [self.judge(assignment, note) for assignment, note in pairs]


def cohens_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Chance-corrected agreement between two binary annotators.

    Raw agreement is misleading when the classes are imbalanced -- if 90% of pairs
    are irrelevant, an annotator that says "irrelevant" every time scores 90%.
    Kappa subtracts the agreement you would expect from chance alone.
    Rough reading: <0.4 poor, 0.4-0.6 moderate, 0.6-0.8 substantial, >0.8 strong.
    """
    if len(a) != len(b) or not a:
        return 0.0
    n = len(a)
    observed = sum(1 for x, y in zip(a, b) if x == y) / n

    a_true = sum(a) / n
    b_true = sum(b) / n
    expected = a_true * b_true + (1 - a_true) * (1 - b_true)

    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def agreement(
    judgements: Sequence[Judgement], hand_labels: dict[tuple[int, int], bool]
) -> dict:
    """Compare judge output with hand labels on the pairs both cover."""
    judge_values: list[bool] = []
    human_values: list[bool] = []
    disagreements: list[dict] = []

    for judgement in judgements:
        key = (judgement.assignment_id, judgement.note_id)
        if key not in hand_labels:
            continue
        human = hand_labels[key]
        judge_values.append(judgement.relevant)
        human_values.append(human)
        if judgement.relevant != human:
            disagreements.append(
                {
                    "assignment_id": judgement.assignment_id,
                    "note_id": judgement.note_id,
                    "human": human,
                    "judge_grade": judgement.grade,
                    "judge_reason": judgement.reason,
                }
            )

    if not judge_values:
        return {"compared": 0, "raw_agreement": 0.0, "cohens_kappa": 0.0, "disagreements": []}

    raw = sum(1 for x, y in zip(judge_values, human_values) if x == y) / len(judge_values)
    return {
        "compared": len(judge_values),
        "raw_agreement": round(raw, 4),
        "cohens_kappa": round(cohens_kappa(judge_values, human_values), 4),
        "disagreements": disagreements,
    }
