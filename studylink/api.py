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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

from . import auth as auth_module
from . import ratelimit
from .agent import AgentUnavailable, citation_coverage
from .canvas import CanvasError
from .context import UserContext
from .errors import CrossUserAccessError, NotFoundError
from .config import load_settings
from .db import create_all, make_engine
from .pgvector_support import apply_search_tuning
from . import store
from .service import StudyLink

api = FastAPI(title="StudyLink", version="0.1.0")

# The engine is process-wide and its pool is shared; connections are not. Each
# request checks one out and returns it, which is what lets `app.user` be a
# per-request fact instead of mutable state on a shared object that two
# concurrent callers would race over. That race would be an authorisation bug,
# not a performance one.
_engine = None


def engine():
    global _engine
    if _engine is None:
        settings = load_settings()
        _engine = make_engine(settings.sqlalchemy_url)
        create_all(_engine)
    return _engine


def db_connection():
    conn = engine().connect()
    apply_search_tuning(conn)  # per-session GUC; no-op off Postgres
    try:
        yield conn
    finally:
        conn.close()


def current_user(
    authorization: Optional[str] = Header(default=None),
    conn=Depends(db_connection),
) -> UserContext:
    """Resolve the bearer token into an identity, or refuse the request.

    The single gate. No anonymous fallback and no default user: an endpoint that
    depends on this either has a real authenticated caller or never runs.

    A dependency rather than a helper the endpoint calls, because forgetting a
    dependency changes the signature where a reviewer sees it, while forgetting
    a call inside a body is invisible.
    """
    token = auth_module.bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    context = auth_module.context_for_token(conn, token)
    if context is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return context


def current_app(
    user: UserContext = Depends(current_user),
    conn=Depends(db_connection),
) -> StudyLink:
    """A service object scoped to this request and this caller."""
    return StudyLink(user=user, conn=conn)


class NoteIn(BaseModel):
    title: str
    body: str
    course_id: Optional[int] = None
    source_type: str = Field(default="note", pattern="^(note|transcript)$")


class SignupIn(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


class WorkSessionIn(BaseModel):
    assignment_id: int
    mode: str = Field(default="outline", pattern="^(outline|draft|summary)$")
    top_k: Optional[int] = None


def _enforce(rule, key: str) -> None:
    """Refuse the request if `key` has run out of budget under `rule`."""
    decision = ratelimit.limiter.check(key, rule)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(int(decision.retry_after) + 1)},
        )


@api.post("/auth/signup", status_code=201)
def signup(
    payload: SignupIn,
    request: Request,
    conn=Depends(db_connection),
) -> dict:
    _enforce(ratelimit.SIGNUP_PER_IP, f"signup:ip:{ratelimit.client_ip(request)}")
    try:
        result = auth_module.signup(
            conn,
            payload.email,
            payload.password,
            display_name=payload.display_name,
            user_agent=request.headers.get("user-agent"),
        )
    except auth_module.SignupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "token": result.token,
        "user": {"id": result.user.user_id, "email": result.user.email},
    }


@api.post("/auth/login")
def login(
    payload: LoginIn,
    request: Request,
    conn=Depends(db_connection),
) -> dict:
    ip_key = f"login:ip:{ratelimit.client_ip(request)}"
    account_key = f"login:account:{store.normalise_email(payload.email)}"

    _enforce(ratelimit.LOGIN_PER_IP, ip_key)

    # Checked but not consumed here: the account budget is spent only by
    # failures, below. Counting successes would let an attacker lock out an
    # account whose owner is legitimately using it.
    if ratelimit.limiter.peek(account_key, ratelimit.LOGIN_PER_ACCOUNT) >= (
        ratelimit.LOGIN_PER_ACCOUNT.limit
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": "60"},
        )

    try:
        result = auth_module.login(
            conn,
            payload.email,
            payload.password,
            user_agent=request.headers.get("user-agent"),
        )
    except auth_module.AuthError as exc:
        ratelimit.limiter.check(account_key, ratelimit.LOGIN_PER_ACCOUNT)
        # One status and one message for every kind of failure -- see auth.py.
        raise HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # A correct password clears the IP budget, so one person fat-fingering their
    # password a few times then getting it right does not stay penalised.
    ratelimit.limiter.reset(ip_key)

    return {
        "token": result.token,
        "user": {"id": result.user.user_id, "email": result.user.email},
    }


@api.post("/auth/logout")
def logout(
    authorization: Optional[str] = Header(default=None),
    conn=Depends(db_connection),
) -> dict:
    """Ends the current session.

    Always 200, even for a token that was already dead. "Log me out" has
    succeeded either way, and reporting the difference would tell an unauthorised
    caller whether the token they hold is live.
    """
    token = auth_module.bearer_token(authorization)
    ended = auth_module.logout(conn, token) if token else False
    return {"ok": True, "ended": ended}


@api.get("/auth/me")
def me(user: UserContext = Depends(current_user)) -> dict:
    return {
        "id": user.user_id,
        "email": user.email,
        "auth_source": user.auth_source,
    }


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
def health(app: StudyLink = Depends(current_app)) -> dict:
    status = app.status()
    return {
        "ok": True,
        "provider": status.provider,
        "assignments": status.assignments,
        "notes": status.notes,
        "index_stale": status.index_stale,
        "ready_for_matching": status.ready_for_matching,
    }


@api.get("/courses")
def list_courses(app: StudyLink = Depends(current_app)) -> list[dict]:
    return [
        {"id": c.id, "name": c.name, "course_code": c.course_code}
        for c in app.list_courses()
    ]


@api.get("/assignments")
def list_assignments(
    course_id: Optional[int] = None, app: StudyLink = Depends(current_app)
) -> list[dict]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "course": a.course_name,
            "due_at": a.due_at,
            "points_possible": a.points_possible,
        }
        for a in app.list_assignments(course_id)
    ]


@api.get("/assignments/{assignment_id}/matches")
def assignment_matches(
    assignment_id: int,
    top_k: Optional[int] = None,
    app: StudyLink = Depends(current_app),
) -> dict:
    matches = app.matches_for_assignment(assignment_id, top_k=top_k)
    assignment = app.get_assignment(assignment_id)
    return {
        "assignment": {"id": assignment.id, "name": assignment.name},
        "matches": [m.as_dict() for m in matches],
    }


@api.get("/notes")
def list_notes(
    course_id: Optional[int] = None,
    search: str = "",
    app: StudyLink = Depends(current_app),
) -> list[dict]:
    return [
        {
            "id": n.id,
            "title": n.title,
            "course": n.course_name,
            "source_type": n.source_type,
            "chars": len(n.body),
        }
        for n in app.list_notes(course_id=course_id, search=search)
    ]


@api.post("/notes", status_code=201)
def create_note(payload: NoteIn, app: StudyLink = Depends(current_app)) -> dict:
    note_id = app.add_note(
        payload.title, payload.body, payload.course_id, payload.source_type
    )
    return {"id": note_id}


@api.get("/notes/{note_id}/assignments")
def reverse_lookup(
    note_id: int, top_k: int = 5, app: StudyLink = Depends(current_app)
) -> list[dict]:
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
def search(
    q: str, top_k: int = 5, app: StudyLink = Depends(current_app)
) -> list[dict]:
    return [m.as_dict() for m in app.search_notes(q, top_k=top_k)]


@api.post("/sync")
def sync(app: StudyLink = Depends(current_app)) -> dict:
    try:
        result = app.sync_canvas()
    except CanvasError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"courses": result.courses, "assignments": result.assignments, "errors": result.errors}


@api.post("/reindex")
def reindex(
    force: bool = False, app: StudyLink = Depends(current_app)
) -> dict:
    stats = app.reindex(force=force)
    return {
        "notes_chunked": stats.notes_chunked,
        "chunks_written": stats.chunks_written,
        "chunk_vectors": stats.chunk_vectors,
        "assignment_vectors": stats.assignment_vectors,
    }


@api.post("/work-session")
def work_session(
    payload: WorkSessionIn, app: StudyLink = Depends(current_app)
) -> dict:
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
def evaluation(app: StudyLink = Depends(current_app)) -> dict:
    return app.evaluate().as_dict()
