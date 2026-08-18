"""Measuring whether the chat layer actually stays grounded.

The retrieval eval answers "did the right notes come back". This answers the
question stacked on top of it: **given those notes, did the answer stay inside
them.** They fail independently -- perfect retrieval with an ungrounded answer
is still a student revising from something they never wrote.

Three metrics, and the third is the one people forget:

`citation_validity` -- of the note ids cited, how many were actually supplied.
An invented id is the worst failure mode here because it looks like evidence.

`citation_coverage` -- how many claims carry a citation at all. An answer with
one citation and six paragraphs is not grounded, it is decorated.

`refusal_accuracy` -- on questions the corpus genuinely cannot answer, how often
the system declines. Without this the other two are trivially gamed by a system
that answers everything confidently from three notes it half-matched. A model
that never refuses scores 0 here, and that is the number that catches it.

Cases live in `data/eval/groundedness.json` so they can be extended without
touching code. Each names a question, whether the corpus can answer it, and
optionally which notes an answer ought to draw on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..chat import Answer, NoteChat

# A sentence long enough to be making a claim. Shorter fragments are usually
# connective tissue, and demanding a citation on "In short:" would penalise
# writing rather than measure grounding.
MIN_CLAIM_CHARS = 40

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CITATION = re.compile(r"\[N\d+\]")


@dataclass
class GroundednessCase:
    question: str
    answerable: bool
    expect_note_ids: list[int] = field(default_factory=list)
    note: str = ""


@dataclass
class CaseResult:
    case: GroundednessCase
    answer: Answer
    cited_valid: int = 0
    cited_invented: int = 0
    claims: int = 0
    claims_cited: int = 0

    @property
    def refused_correctly(self) -> Optional[bool]:
        """True/False on unanswerable questions, None where it does not apply."""
        if self.case.answerable:
            return None
        return self.answer.refused

    @property
    def answered_when_it_should_have(self) -> Optional[bool]:
        if not self.case.answerable:
            return None
        return not self.answer.refused


@dataclass
class GroundednessReport:
    results: list[CaseResult]

    def _mean(self, values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    @property
    def citation_validity(self) -> float:
        cited = sum(r.cited_valid + r.cited_invented for r in self.results)
        if not cited:
            return 1.0  # nothing cited, nothing invented
        return round(sum(r.cited_valid for r in self.results) / cited, 4)

    @property
    def citation_coverage(self) -> float:
        claims = sum(r.claims for r in self.results)
        if not claims:
            return 0.0
        return round(sum(r.claims_cited for r in self.results) / claims, 4)

    @property
    def refusal_accuracy(self) -> float:
        """How often an unanswerable question was actually declined.

        A system that answers everything scores 0 here while looking fine on the
        other two, which is exactly why this metric exists.
        """
        judged = [r.refused_correctly for r in self.results if r.refused_correctly is not None]
        return self._mean([1.0 if ok else 0.0 for ok in judged])

    @property
    def answer_rate(self) -> float:
        """How often an answerable question got an answer rather than a refusal.

        The counterweight to refusal_accuracy: a system that refuses everything
        scores 1.0 there and 0.0 here.
        """
        judged = [
            r.answered_when_it_should_have
            for r in self.results
            if r.answered_when_it_should_have is not None
        ]
        return self._mean([1.0 if ok else 0.0 for ok in judged])

    def as_dict(self) -> dict:
        return {
            "cases": len(self.results),
            "citation_validity": self.citation_validity,
            "citation_coverage": self.citation_coverage,
            "refusal_accuracy": self.refusal_accuracy,
            "answer_rate": self.answer_rate,
            "invented_citations": sum(r.cited_invented for r in self.results),
        }


def split_claims(text: str) -> list[str]:
    """Sentences substantial enough to be making a claim."""
    return [
        sentence.strip()
        for sentence in _SENTENCE.split(text or "")
        if len(sentence.strip()) >= MIN_CLAIM_CHARS
    ]


def score_answer(case: GroundednessCase, answer: Answer) -> CaseResult:
    claims = split_claims(answer.text)
    return CaseResult(
        case=case,
        answer=answer,
        cited_valid=len(answer.cited_note_ids),
        cited_invented=len(answer.invented_note_ids),
        claims=len(claims),
        claims_cited=sum(1 for claim in claims if _CITATION.search(claim)),
    )


def load_cases(path: Path) -> list[GroundednessCase]:
    payload = json.loads(Path(path).read_text())
    return [
        GroundednessCase(
            question=entry["question"],
            answerable=bool(entry.get("answerable", True)),
            expect_note_ids=list(entry.get("expect_note_ids", [])),
            note=entry.get("note", ""),
        )
        for entry in payload
    ]


def evaluate(chat: NoteChat, cases: list[GroundednessCase]) -> GroundednessReport:
    results = []
    for case in cases:
        answer = chat.ask(case.question)
        results.append(score_answer(case, answer))
    return GroundednessReport(results=results)


def format_report(report: GroundednessReport) -> str:
    metrics = report.as_dict()
    lines = [
        f"cases:               {metrics['cases']}",
        f"citation validity:   {metrics['citation_validity']:.3f}  "
        f"(invented ids: {metrics['invented_citations']})",
        f"citation coverage:   {metrics['citation_coverage']:.3f}",
        f"refusal accuracy:    {metrics['refusal_accuracy']:.3f}  "
        "(declines when the notes cannot answer)",
        f"answer rate:         {metrics['answer_rate']:.3f}  "
        "(answers when they can)",
        "",
        "per case:",
    ]
    for result in report.results:
        verdict = "refused" if result.answer.refused else "answered"
        expected = "unanswerable" if not result.case.answerable else "answerable"
        flag = ""
        if result.case.answerable and result.answer.refused:
            flag = "   <-- should have answered"
        if not result.case.answerable and not result.answer.refused:
            flag = "   <-- should have refused"
        if result.cited_invented:
            flag += f"   <-- {result.cited_invented} invented citation(s)"
        lines.append(
            f"  {result.case.question[:52]:<52} {expected:<12} {verdict}{flag}"
        )
    return "\n".join(lines)
