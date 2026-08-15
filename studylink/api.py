"""FastAPI wrapper over the same service object the Streamlit app uses.

    uvicorn studylink.api:api --reload

This exists so the retrieval and agent layers are reachable without the UI --
useful for scripting, for a future frontend, and for showing that the core is not
entangled with Streamlit. It is intentionally thin: every endpoint is a couple of
lines over `StudyLink`.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from .agent import AgentUnavailable, citation_coverage
from .canvas import CanvasError
from .context import UserContext
from .errors import CrossUserAccessError, NotFoundError
from .service import StudyLink

api = FastAPI(title="StudyLink", version="0.1.0")
_app: Optional[StudyLink] = None


def get_app() -> StudyLink:
    """The service for the current caller.

    Day 3 replaces this with a FastAPI dependency that builds a UserContext from
    the request's bearer token. Until then every request is the local user, and
    that assumption lives in exactly one place.
    """
    global _app
    if _app is None:
        app = StudyLink()
        app.user = UserContext.local(app.conn)
        _app = app
    return _app


class NoteIn(BaseModel):
    title: str
    body: str
    course_id: Optional[int] = None
    source_type: str = Field(default="note", pattern="^(note|transcript)$")


class WorkSessionIn(BaseModel):
    assignment_id: int
    mode: str = Field(default="outline", pattern="^(outline|draft|summary)$")
    top_k: Optional[int] = None


@api.exception_handler(CrossUserAccessError)
def _cross_user(request, exc: CrossUserAccessError):
    """Deliberately a 404, not a 403.

    A 403 tells the caller the row exists and belongs to somebody else, which is
    precisely the fact worth hiding. The event is logged server-side with both
    user ids; the client learns only that there is nothing here for them.
    """
    logger.warning(
        "cross-user access blocked: %s %s owned by %s, requested by %s",
        exc.table, exc.row_id, exc.owner_id, exc.actor_id,
    )
    return JSONResponse(status_code=404, content={"detail": "not found"})


@api.exception_handler(NotFoundError)
def _not_found(request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": "not found"})


@api.get("/health")
def health() -> dict:
    status = get_app().status()
    return {
        "ok": True,
        "provider": status.provider,
        "assignments": status.assignments,
        "notes": status.notes,
        "index_stale": status.index_stale,
        "ready_for_matching": status.ready_for_matching,
    }


@api.get("/courses")
def list_courses() -> list[dict]:
    return [
        {"id": c.id, "name": c.name, "course_code": c.course_code}
        for c in get_app().list_courses()
    ]


@api.get("/assignments")
def list_assignments(course_id: Optional[int] = None) -> list[dict]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "course": a.course_name,
            "due_at": a.due_at,
            "points_possible": a.points_possible,
        }
        for a in get_app().list_assignments(course_id)
    ]


@api.get("/assignments/{assignment_id}/matches")
def assignment_matches(assignment_id: int, top_k: Optional[int] = None) -> dict:
    app = get_app()
    matches = app.matches_for_assignment(assignment_id, top_k=top_k)
    assignment = app.get_assignment(assignment_id)
    return {
        "assignment": {"id": assignment.id, "name": assignment.name},
        "matches": [m.as_dict() for m in matches],
    }


@api.get("/notes")
def list_notes(course_id: Optional[int] = None, search: str = "") -> list[dict]:
    return [
        {
            "id": n.id,
            "title": n.title,
            "course": n.course_name,
            "source_type": n.source_type,
            "chars": len(n.body),
        }
        for n in get_app().list_notes(course_id=course_id, search=search)
    ]


@api.post("/notes", status_code=201)
def create_note(payload: NoteIn) -> dict:
    note_id = get_app().add_note(
        payload.title, payload.body, payload.course_id, payload.source_type
    )
    return {"id": note_id}


@api.get("/notes/{note_id}/assignments")
def reverse_lookup(note_id: int, top_k: int = 5) -> list[dict]:
    app = get_app()
    return [
        {
            "assignment_id": m.assignment.id,
            "name": m.assignment.name,
            "course": m.assignment.course_name,
            "score": round(m.score, 4),
            "confidence": m.confidence,
            "evidence": m.evidence.as_dict(),
        }
        for m in app.assignments_for_note(note_id, top_k=top_k)
    ]


@api.get("/search")
def search(q: str, top_k: int = 5) -> list[dict]:
    return [m.as_dict() for m in get_app().search_notes(q, top_k=top_k)]


@api.post("/sync")
def sync() -> dict:
    try:
        result = get_app().sync_canvas()
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"courses": result.courses, "assignments": result.assignments, "errors": result.errors}


@api.post("/reindex")
def reindex(force: bool = False) -> dict:
    stats = get_app().reindex(force=force)
    return {
        "notes_chunked": stats.notes_chunked,
        "chunks_written": stats.chunks_written,
        "chunk_vectors": stats.chunk_vectors,
        "assignment_vectors": stats.assignment_vectors,
    }


@api.post("/work-session")
def work_session(payload: WorkSessionIn) -> dict:
    app = get_app()
    assignment = app.get_assignment(payload.assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found")

    matches = app.matches_for_assignment(payload.assignment_id, top_k=payload.top_k)
    if not matches:
        raise HTTPException(status_code=409, detail="no notes matched this assignment")

    try:
        output, _ = app.agent().synthesize(assignment, matches, payload.mode)
    except AgentUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    offered = [m.note.id for m in matches]
    return {
        "assignment_id": assignment.id,
        "mode": output.mode,
        "output": output.text,
        "matches": [m.as_dict() for m in matches],
        "traceability": citation_coverage(output.text, offered),
    }


@api.get("/evaluation")
def evaluation() -> dict:
    return get_app().evaluate().as_dict()
