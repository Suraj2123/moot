"""The work-session agent.

Given an assignment, the agent receives the retrieved notes up front and can pull
more with a `search_notes` tool, then produces one of three artefacts: a study
outline, a first-draft skeleton, or a deduplicated concept summary. The student
can then keep talking to it in the same context to refine.

Traceability is the hard requirement: every claim must carry a `[N<id>]` citation
pointing at the note it came from, and the UI renders those as links back to the
note. Output that cites nothing is a summary of the model's priors, not of the
student's notes -- which is the failure mode this whole app exists to avoid.

Implementation note: this is a hand-written tool loop rather than the SDK's
`tool_runner`. The runner's tools are decorated module-level functions, and this
tool needs to close over a per-request `Retriever` (which is bound to a specific
database connection and embedding provider). Threading that through a global
would be worse than owning eleven lines of loop.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import store
from .config import AGENT_MODEL
from .models import Assignment, NoteMatch, WorkSessionOutput
from .retrieval import Retriever

CITATION = re.compile(r"\[N(\d+)\]")

MODES = {
    "outline": (
        "Produce a study/work outline: a bullet structure of what this assignment "
        "requires the student to cover, ordered the way they should work through it. "
        "Under each bullet, name the specific note that covers it."
    ),
    "draft": (
        "Produce a first-draft skeleton for this deliverable: section headings, the "
        "claim or content each section needs to make, and the note-backed material "
        "available for it. Write real opening sentences where the notes support them; "
        "mark gaps explicitly as TODO rather than inventing content."
    ),
    "summary": (
        "Produce a deduplicated summary of the concepts across the matched notes that "
        "are relevant to this assignment. Where two notes cover the same concept, merge "
        "them into one entry and cite both. Group by concept, not by note."
    ),
}

SYSTEM_PROMPT = """You are StudyLink's work-session assistant. A student has an assignment \
and a set of their own notes and lecture material that were retrieved as relevant to it. \
Your job is to turn those into something they can start working from.

Rules:
- Ground everything in the supplied notes. Cite the note you used inline as [N<id>], \
using the ids given in the context (e.g. [N7]). Cite the specific note for each point, \
not a block of ids at the end.
- If the notes do not cover something the assignment requires, say so explicitly and mark \
it as a gap. Do not fill gaps from your own knowledge and present it as the student's \
material. You may briefly flag what they would need to look up.
- If the retrieved notes look off-topic for the assignment, say that plainly first -- \
that is useful signal that the matching went wrong.
- Use the search_notes tool when the student asks about something the initial context \
does not cover, or when you need more detail on a specific concept.
- Write for the student, not about your process. No preamble about what you are about to do.

Lead with the outcome. Keep the output as long as the material warrants and no longer."""

SEARCH_TOOL = {
    "name": "search_notes",
    "description": (
        "Semantic search across the student's notes and lecture transcripts. Use this when "
        "the assignment or the student's question touches on something the initial matched "
        "notes do not cover in enough detail. Returns note ids you can cite as [N<id>]."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, phrased as the concept or topic, not as a question.",
            },
            "top_k": {
                "type": "integer",
                "description": "How many notes to return. Default 4, max 10.",
            },
        },
        "required": ["query"],
    },
}


class AgentUnavailable(RuntimeError):
    """Raised when no Anthropic credentials are configured."""


@dataclass
class AgentResult:
    text: str
    cited_note_ids: list[int] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    refused: bool = False


def extract_citations(text: str) -> list[int]:
    """Note ids cited in the output, in first-appearance order."""
    seen: list[int] = []
    for match in CITATION.finditer(text or ""):
        note_id = int(match.group(1))
        if note_id not in seen:
            seen.append(note_id)
    return seen


def format_note_context(matches: list[NoteMatch], max_chars: int = 2400) -> str:
    """Render retrieved notes into the block the agent reads.

    The matched chunk leads, because it is the part retrieval actually scored. The
    rest of the note follows up to a budget, so the agent sees surrounding context
    without a single long note crowding out the others.
    """
    blocks = []
    for match in matches:
        note = match.note
        body = note.body.strip()
        matched = match.matched_chunk.text.strip() if match.matched_chunk else ""

        if len(body) > max_chars:
            body = body[:max_chars].rsplit(" ", 1)[0] + " [...truncated]"

        header = f"[N{note.id}] {note.title}"
        meta = f"course: {note.course_name or 'unassigned'} | type: {note.source_type} | match confidence: {match.confidence:.2f}"
        block = f"{header}\n{meta}\n"
        if matched and matched not in body:
            block += f"MOST RELEVANT PASSAGE:\n{matched}\n\n"
        block += f"FULL NOTE:\n{body}"
        blocks.append(block)

    return "\n\n---\n\n".join(blocks) if blocks else "(no notes matched this assignment)"


def format_assignment_context(assignment: Assignment) -> str:
    parts = [
        f"Assignment: {assignment.name}",
        f"Course: {assignment.course_name}",
    ]
    if assignment.due_at:
        parts.append(f"Due: {assignment.due_at}")
    if assignment.points_possible is not None:
        parts.append(f"Points: {assignment.points_possible:g}")
    if assignment.submission_types:
        parts.append(f"Submission type: {assignment.submission_types}")
    parts.append(f"\nDescription:\n{assignment.description or '(no description provided)'}")
    return "\n".join(parts)


class WorkSessionAgent:
    def __init__(
        self,
        conn: sqlite3.Connection,
        retriever: Retriever,
        client=None,
        model: str = AGENT_MODEL,
        max_tokens: int = 16000,
    ) -> None:
        self.conn = conn
        self.retriever = retriever
        self.model = model
        self.max_tokens = max_tokens
        self._client = client
        # Server-side refusal fallback: on a policy decline the API re-runs the
        # request on Anthropic's recommended fallback model inside the same call,
        # so a benign request near a classifier boundary still gets answered.
        self._use_fallbacks = True

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise AgentUnavailable("The `anthropic` package is not installed.") from exc
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:
                raise AgentUnavailable(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY."
                ) from exc
        return self._client

    # ------------------------------------------------------------------ tool impl

    def _run_search(self, tool_input: dict) -> str:
        query = str(tool_input.get("query", "")).strip()
        top_k = int(tool_input.get("top_k") or 4)
        top_k = max(1, min(top_k, 10))
        if not query:
            return "No query supplied."

        matches = self.retriever.search_notes(query, top_k=top_k, apply_threshold=False)
        if not matches:
            return "No notes matched that query."

        lines = []
        for match in matches:
            snippet = (match.matched_chunk.text if match.matched_chunk else match.evidence.snippet)
            snippet = snippet.strip()[:900]
            lines.append(
                f"[N{match.note.id}] {match.note.title} "
                f"(course: {match.note.course_name or 'unassigned'}, confidence {match.confidence:.2f})\n{snippet}"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ API calls

    def _create(self, messages: list[dict], system: str):
        """One Messages API call, with the tool and refusal fallback attached."""
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": [SEARCH_TOOL],
            "output_config": {"effort": "high"},
        }
        if self._use_fallbacks:
            try:
                with self.client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **kwargs,
                ) as stream:
                    return stream.get_final_message()
            except TypeError:
                # SDK too old for the `fallbacks` parameter -- fall through and
                # stop trying on subsequent calls.
                self._use_fallbacks = False

        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def _run_loop(
        self,
        messages: list[dict],
        system: str,
        max_iterations: int = 6,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
    ) -> AgentResult:
        tool_calls: list[dict] = []

        for _ in range(max_iterations):
            response = self._create(messages, system)

            if response.stop_reason == "refusal":
                return AgentResult(
                    text=(
                        "The request was declined by Anthropic's safety classifiers, and the "
                        "fallback model declined as well. Try rephrasing the assignment prompt."
                    ),
                    stop_reason="refusal",
                    refused=True,
                )

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                return AgentResult(
                    text=text.strip(),
                    cited_note_ids=extract_citations(text),
                    tool_calls=tool_calls,
                    stop_reason=response.stop_reason,
                )

            # Echo the assistant turn back verbatim -- thinking and tool_use blocks
            # must survive the round trip.
            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls.append({"name": block.name, "input": block.input})
                if on_tool_call:
                    on_tool_call(block.name, dict(block.input))
                output = (
                    self._run_search(dict(block.input))
                    if block.name == "search_notes"
                    else f"Unknown tool {block.name}."
                )
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
            messages.append({"role": "user", "content": results})

        return AgentResult(
            text="The agent kept searching without settling on an answer. Try a narrower request.",
            tool_calls=tool_calls,
            stop_reason="max_iterations",
        )

    # --------------------------------------------------------------------- public

    def build_initial_prompt(
        self, assignment: Assignment, matches: list[NoteMatch], mode: str
    ) -> str:
        instruction = MODES.get(mode, MODES["outline"])
        return (
            f"{format_assignment_context(assignment)}\n\n"
            f"=== RETRIEVED NOTES ===\n{format_note_context(matches)}\n\n"
            f"=== TASK ===\n{instruction}"
        )

    def synthesize(
        self,
        assignment: Assignment,
        matches: list[NoteMatch],
        mode: str = "outline",
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
    ) -> tuple[WorkSessionOutput, list[dict]]:
        """Produce the opening artefact for a work session.

        Returns the output plus the message history, so the caller can keep
        chatting in the same context.
        """
        messages = [{"role": "user", "content": self.build_initial_prompt(assignment, matches, mode)}]
        result = self._run_loop(messages, SYSTEM_PROMPT, on_tool_call=on_tool_call)
        messages.append({"role": "assistant", "content": result.text})
        output = WorkSessionOutput(
            text=result.text, cited_note_ids=result.cited_note_ids, mode=mode
        )
        return output, messages

    def refine(
        self,
        messages: list[dict],
        user_message: str,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
    ) -> tuple[AgentResult, list[dict]]:
        """Continue an existing work session with a follow-up instruction."""
        messages = list(messages)
        messages.append({"role": "user", "content": user_message})
        result = self._run_loop(messages, SYSTEM_PROMPT, on_tool_call=on_tool_call)
        messages.append({"role": "assistant", "content": result.text})
        return result, messages


# ------------------------------------------------------------------- persistence


def save_session(conn: sqlite3.Connection, assignment_id: int, mode: str) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute(
        "INSERT INTO work_sessions (assignment_id, mode, created_at) VALUES (?, ?, ?)",
        (assignment_id, mode, now),
    )
    conn.commit()
    return int(cursor.lastrowid)


def save_message(conn: sqlite3.Connection, session_id: int, role: str, content: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO work_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, now),
    )
    conn.commit()


def load_messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM work_messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def resolve_citations(conn: sqlite3.Connection, note_ids: list[int]) -> list[dict]:
    """Turn cited ids into displayable references, flagging any that do not exist."""
    notes = store.get_notes(conn, note_ids)
    out = []
    for note_id in note_ids:
        note = notes.get(note_id)
        out.append(
            {
                "note_id": note_id,
                "title": note.title if note else f"(unknown note {note_id})",
                "course": note.course_name if note else "",
                "valid": note is not None,
            }
        )
    return out


def citation_coverage(text: str, offered_note_ids: list[int]) -> dict:
    """A cheap traceability check on agent output.

    `hallucinated_ids` is the important one: ids the agent cited that were never
    offered to it. A non-empty list means the output is claiming provenance it
    does not have.
    """
    cited = extract_citations(text)
    offered = set(offered_note_ids)
    return {
        "cited": cited,
        "unused_offered": sorted(offered - set(cited)),
        "hallucinated_ids": [note_id for note_id in cited if note_id not in offered],
        "citation_count": len(CITATION.findall(text or "")),
    }
