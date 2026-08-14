"""Plain dataclasses passed between the storage layer, the retriever, and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Course:
    id: int
    canvas_id: str
    name: str
    course_code: str = ""
    synced_at: Optional[str] = None


@dataclass
class Assignment:
    id: int
    course_id: int
    canvas_id: str
    name: str
    description: str = ""
    due_at: Optional[str] = None
    points_possible: Optional[float] = None
    submission_types: str = ""
    html_url: Optional[str] = None
    course_name: str = ""

    @property
    def retrieval_text(self) -> str:
        """The text used to build this assignment's embedding.

        The title is repeated because it is short and unusually information
        dense compared with the description boilerplate Canvas ships with most
        assignments ("Submit as a PDF", "Late work is penalised", etc.).
        """
        return f"{self.name}\n{self.name}\n{self.description}".strip()

    @property
    def is_written_deliverable(self) -> bool:
        types = (self.submission_types or "").lower()
        return any(t in types for t in ("online_text_entry", "online_upload", "discussion_topic"))


@dataclass
class Note:
    id: int
    title: str
    body: str
    course_id: Optional[int] = None
    source_type: str = "note"
    created_at: str = ""
    course_name: str = ""


@dataclass
class Chunk:
    id: int
    note_id: int
    ordinal: int
    text: str


@dataclass
class Evidence:
    """Why a note matched -- the part of the UI that keeps this from being a black box."""

    snippet: str
    overlapping_terms: list[str] = field(default_factory=list)
    chunk_ordinal: int = 0

    def as_dict(self) -> dict:
        return {
            "snippet": self.snippet,
            "overlapping_terms": self.overlapping_terms,
            "chunk_ordinal": self.chunk_ordinal,
        }


@dataclass
class NoteMatch:
    """A note retrieved for an assignment (or vice versa)."""

    note: Note
    score: float           # raw cosine similarity of the best-matching chunk
    confidence: float      # 0-1, calibrated for display
    evidence: Evidence
    matched_chunk: Optional[Chunk] = None

    def as_dict(self) -> dict:
        return {
            "note_id": self.note.id,
            "title": self.note.title,
            "course": self.note.course_name,
            "source_type": self.note.source_type,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence.as_dict(),
        }


@dataclass
class AssignmentMatch:
    """Reverse lookup: an assignment a given note is relevant to."""

    assignment: Assignment
    score: float
    confidence: float
    evidence: Evidence


@dataclass
class WorkSessionOutput:
    text: str
    cited_note_ids: list[int] = field(default_factory=list)
    mode: str = "outline"
