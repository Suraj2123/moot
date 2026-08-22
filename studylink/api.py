"""FastAPI wrapper over the same service object the Streamlit app uses.

    uvicorn studylink.api:api --reload

This exists so the retrieval and agent layers are reachable without the UI --
useful for scripting, for a future frontend, and for showing that the core is not
entangled with Streamlit. It is intentionally thin: every endpoint is a couple of
lines over `StudyLink`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy import text
from fastapi.responses import (
    FileResponse,
    Response,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)

from . import auth as auth_module
from . import cards as cards_module
from . import credentials
from . import documents
from . import outline
from . import jobs as jobs_module
from . import preflight
from . import usage as usage_module
from . import ratelimit
from . import sessions as sessions_module
from .agent import AgentUnavailable, citation_coverage
from .canvas import CanvasError
from .context import UserContext
from .errors import CrossUserAccessError, NotFoundError
from .config import load_settings
from .db import create_all, make_engine
from .pgvector_support import apply_search_tuning
from . import store
from .service import StudyLink

# Checked at import, so a misconfigured deployment fails at boot -- where the
# platform surfaces it -- rather than on the first request that needs the
# missing piece. Fatal in production, advisory in development.
preflight.run()

api = FastAPI(title="moot", version="0.1.0")

# The engine is process-wide and its pool is shared; connections are not. Each
# request checks one out and returns it, which is what lets `app.user` be a
# per-request fact instead of mutable state on a shared object that two
# concurrent callers would race over. That race would be an authorisation bug,
# not a performance one.
# Browser origins allowed to call this API. Empty by default: a same-origin
# frontend needs no CORS at all, and a wildcard would let any page on the
# internet make authenticated calls with a user's token. This has to be an
# explicit deployment decision.
def allowed_origins() -> list[str]:
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@api.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers that matter for an API serving a browser frontend.

    Not a Content-Security-Policy: this serves JSON, and a CSP belongs on the
    document that loads the scripts, not on the API the scripts call. Claiming
    otherwise here would be security theatre in a header nobody enforces.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # Tokens live in the Authorization header rather than a cookie, so CSRF is
    # not the threat it would otherwise be -- but a stray <iframe> embedding an
    # error page is still worth refusing.
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Responses are per-user. A shared cache holding one user's notes and
    # serving them to the next is the failure this prevents.
    response.headers.setdefault("Cache-Control", "no-store")
    return response


_engine = None


def engine():
    global _engine
    if _engine is None:
        settings = load_settings()
        _engine = make_engine(settings.sqlalchemy_url)
        create_all(_engine)
    return _engine


_origins = allowed_origins()
if _origins:
    from fastapi.middleware.cors import CORSMiddleware

    api.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        # Credentials are sent as a bearer header, not a cookie, so this stays
        # off: turning it on with an explicit origin list is what makes a
        # browser attach cookies to cross-origin calls, and nothing here wants
        # that.
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


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


class NotePatchIn(BaseModel):
    """Every field optional: a PATCH says what changed, not what the note is.

    `course_id` needs three states -- leave alone, set, and unset -- which one
    optional integer cannot carry, so unsetting is its own flag rather than a
    magic value.
    """

    title: Optional[str] = None
    body: Optional[str] = None
    course_id: Optional[int] = None
    clear_course: bool = False


class SignupIn(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


class AskIn(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list)


class CanvasConnectIn(BaseModel):
    api_url: str
    api_token: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class DeckIn(BaseModel):
    note_id: int
    count: int = Field(default=10, ge=1, le=20)
    title: Optional[str] = None


class ReviewIn(BaseModel):
    """0 forgot, 1 hard, 2 good, 3 easy."""

    grade: int = Field(ge=0, le=3)


class OutlinePreviewIn(BaseModel):
    text: str


class WrittenAnswerIn(BaseModel):
    given: str
    expected: str


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


@api.post("/auth/password")
def change_password(
    payload: PasswordChangeIn,
    authorization: Optional[str] = Header(default=None),
    user: UserContext = Depends(current_user),
    conn=Depends(db_connection),
) -> dict:
    """Change the password and sign out everywhere else."""
    token = auth_module.bearer_token(authorization)
    try:
        ended = auth_module.change_password(
            conn,
            user.user_id,
            payload.current_password,
            payload.new_password,
            keep_token=token,
        )
    except auth_module.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except auth_module.SignupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, "sessions_ended": ended}


@api.get("/auth/sessions")
def list_sessions(
    user: UserContext = Depends(current_user),
    conn=Depends(db_connection),
) -> list[dict]:
    """The caller's own live sessions. Never anyone else's, and no token hashes."""
    return sessions_module.list_for_user(conn, user.user_id)


@api.delete("/auth/sessions/{session_id}")
def revoke_session(
    session_id: int,
    user: UserContext = Depends(current_user),
    conn=Depends(db_connection),
) -> dict:
    """End one of the caller's sessions.

    404 rather than 403 when the session belongs to somebody else, for the same
    reason cross-user row access is a 404: a 403 confirms it exists.
    """
    if not sessions_module.revoke_by_id(conn, session_id, user.user_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@api.post("/auth/sessions/revoke-others")
def revoke_other_sessions(
    authorization: Optional[str] = Header(default=None),
    user: UserContext = Depends(current_user),
    conn=Depends(db_connection),
) -> dict:
    """Sign out everywhere else, keeping the session making this request."""
    token = auth_module.bearer_token(authorization)
    ended = sessions_module.revoke_others(conn, user.user_id, token or "")
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


# ------------------------------------------------------------------ the site
#
# The built frontend is served by the same process as the API. One thing to
# deploy, one origin, and therefore no CORS to configure in production -- the
# CORS settings exist for a separate dev server, not for the shipped app.

STATIC_DIR = Path(__file__).resolve().parent / "static"


@api.get("/", include_in_schema=False)
def root():
    """Send people to the app, or tell them how to build it."""
    if (STATIC_DIR / "index.html").exists():
        return RedirectResponse("/app/")
    return JSONResponse(
        {
            "detail": "The web UI has not been built yet.",
            "build": "cd web && npm install && npm run build",
            "api_docs": "/docs",
        }
    )


@api.get("/app/{path:path}", include_in_schema=False)
def spa(path: str):
    """Serve the built app, falling back to index.html.

    The fallback is what makes client-side routing work: any path under /app/
    that is not a real file is the app's own route, not a 404. Paths are
    resolved and checked to stay inside the static directory, so `..` segments
    cannot walk out of it and read arbitrary files.
    """
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="The web UI has not been built.")

    if path:
        candidate = (STATIC_DIR / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
            return FileResponse(candidate)

    return FileResponse(index)


@api.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Liveness: is this process running at all.

    Deliberately touches nothing. A liveness probe that queries the database
    restarts a healthy web process every time the database hiccups, which turns
    a brief outage into a restart loop.
    """
    return {"ok": True}


@api.get("/readyz", include_in_schema=False)
def readyz():
    """Readiness: can this process actually serve traffic.

    Unauthenticated on purpose -- a load balancer has no credentials -- and it
    reports only whether dependencies answer, never anything about the data.

    It opens its own connection rather than taking the usual dependency. A
    dependency that raises fails the request before the handler runs, so an
    unreachable database produced a 500 traceback instead of the 503 a load
    balancer is looking for. Reporting unhealthy is the entire job, so this
    endpoint cannot be allowed to crash on the thing it is reporting about.
    """
    checks: dict[str, object] = {}
    healthy = True

    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            checks["migrations"] = "applied" if _migrations_applied(conn) else "pending"
            if checks["migrations"] == "pending":
                healthy = False
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        logger.warning("readiness check failed: %s", exc)
        checks["database"] = f"error: {type(exc).__name__}"
        checks.setdefault("migrations", "unknown")
        healthy = False

    checks["static"] = "built" if (STATIC_DIR / "index.html").exists() else "missing"

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"ok": healthy, **checks},
    )


def _migrations_applied(conn) -> bool:
    """Whether the database is at the migration revision this code expects.

    Serving traffic against a half-migrated database produces errors that look
    like application bugs, so it is better to fail readiness and let the
    platform hold traffic until the release finishes.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        head = ScriptDirectory.from_config(config).get_current_head()
        return current == head
    except Exception:
        # No alembic_version table means create_all built the schema, which is
        # the normal local path and not a reason to fail readiness.
        return True


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
    # One status query for the list, not one per note. "queued" is decided
    # here rather than in the store because it is a fact about the job table,
    # not about the note: a pending reindex means every stale note is about to
    # be picked up, and saying so is the difference between "this is broken"
    # and "this is coming".
    status = app.note_index_status()
    pending = any(
        job.status in {jobs_module.QUEUED, jobs_module.RUNNING}
        and job.kind == jobs_module.KIND_REINDEX
        for job in jobs_module.list_for_user(app.conn, app.user_id)
    )

    def index_state(note_id: int) -> str:
        state = status.get(note_id, "stale")
        if state == "stale" and pending:
            return "queued"
        return state

    return [
        {
            "id": n.id,
            "title": n.title,
            "course": n.course_name,
            "source_type": n.source_type,
            "chars": len(n.body),
            "index_status": index_state(n.id),
        }
        for n in app.list_notes(course_id=course_id, search=search)
    ]


@api.post("/notes", status_code=201)
def create_note(payload: NoteIn, app: StudyLink = Depends(current_app)) -> dict:
    """Save a note and queue the reindex rather than doing it inline.

    Chunking and embedding a note is not slow enough to be alarming today, but
    it grows with the corpus and it is on the path of the most common write in
    the app. The note is durable when this returns; it is searchable once the
    queued job runs.
    """
    note_id = app.add_note(
        payload.title, payload.body, payload.course_id, payload.source_type,
        reindex=False,
    )
    job = jobs_module.enqueue(app.conn, app.user_id, jobs_module.KIND_REINDEX)
    return {"id": note_id, "job": job.as_dict()}


@api.get("/notes/{note_id}")
def get_note(note_id: int, app: StudyLink = Depends(current_app)) -> dict:
    """One note, including its body.

    The list endpoint deliberately omits bodies -- a hundred notes is a lot of
    text to send to render titles -- so editing needs somewhere to fetch the
    text from.
    """
    note = app.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "course": note.course_name,
        "course_id": note.course_id,
        "source_type": note.source_type,
        "chars": len(note.body),
    }


@api.patch("/notes/{note_id}")
def update_note(
    note_id: int, payload: NotePatchIn, app: StudyLink = Depends(current_app)
) -> dict:
    """Edit a note. Queues a reindex only when the text actually changed.

    A rename or a course change leaves every chunk still accurate, and
    reindexing on those would throw away work for nothing.
    """
    changed = app.update_note(
        note_id,
        title=payload.title,
        body=payload.body,
        course_id=payload.course_id,
        clear_course=payload.clear_course,
    )
    job = None
    if changed:
        job = jobs_module.enqueue(app.conn, app.user_id, jobs_module.KIND_REINDEX)
    return {
        "id": note_id,
        "reindexed": changed,
        "job": job.as_dict() if job else None,
    }


@api.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int, app: StudyLink = Depends(current_app)) -> Response:
    """Remove a note, its chunks, and their vectors.

    204 with no body: there is nothing useful to say about a thing that no
    longer exists, and returning the deleted row invites callers to depend on
    it.
    """
    app.delete_note(note_id)
    return Response(status_code=204)


@api.post("/notes/upload", status_code=201)
async def upload_note(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    course_id: Optional[int] = Form(default=None),
    source_type: str = Form(default="note"),
    app: StudyLink = Depends(current_app),
) -> dict:
    """Turn an uploaded document into a note.

    The same shape as POST /notes once the text is out: the note is durable
    when this returns and searchable once the queued job runs.

    Reading is chunked and checked against the cap as it goes. Reading the
    whole upload and measuring afterwards would mean a large file has already
    been in memory by the time it is rejected, which is the opposite of what
    a limit is for.
    """
    if source_type not in {"note", "transcript"}:
        raise HTTPException(status_code=422, detail="source_type must be note or transcript")

    filename = file.filename or ""
    if not documents.is_supported(filename):
        supported = ", ".join(sorted(documents.SUPPORTED))
        raise HTTPException(
            status_code=415,
            detail=f"{documents.suffix_of(filename) or 'That file type'} is not "
                   f"supported. Upload one of: {supported}.",
        )

    chunks: list[bytes] = []
    total = 0
    while piece := await file.read(64 * 1024):
        total += len(piece)
        if total > documents.MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"That file is over the "
                       f"{documents.MAX_BYTES // (1024 * 1024)} MB limit.",
            )
        chunks.append(piece)

    try:
        extracted = documents.extract(filename, b"".join(chunks))
    except documents.DocumentError as exc:
        # 422: the request was well formed and the file is simply not usable.
        # The message is the whole value here -- it is the only thing telling
        # someone their PDF is a scan rather than broken.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    note_id = app.add_note(
        (title or "").strip() or documents.title_from_filename(filename),
        extracted.text,
        course_id,
        source_type,
        reindex=False,
    )
    job = jobs_module.enqueue(app.conn, app.user_id, jobs_module.KIND_REINDEX)
    return {
        "id": note_id,
        "job": job.as_dict(),
        "kind": extracted.kind,
        "chars": len(extracted.text),
        "notice": extracted.notice,
    }


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


def _chat_for(app: StudyLink):
    from .chat import NoteChat

    return NoteChat(app.conn, app.retriever, user_id=app.user_id)


@api.post("/ask")
def ask(payload: AskIn, app: StudyLink = Depends(current_app)) -> dict:
    """Answer a question from this account's notes.

    Returns the answer together with its sources and a `grounded` flag. A client
    that renders the text and ignores `grounded` is choosing to show citations
    it has not checked -- the flag exists so that is a decision rather than an
    oversight.
    """
    try:
        answer = _chat_for(app).ask(payload.question, history=payload.history)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except usage_module.BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AgentUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return answer.as_dict()


@api.post("/ask/stream")
def ask_stream(payload: AskIn, app: StudyLink = Depends(current_app)):
    """The same answer, streamed as server-sent events.

    A grounded reply over six notes takes several seconds, and a chat box that
    shows nothing for that long reads as broken.

    The budget and the question are validated before the response starts. Once
    an SSE stream is open the status code is already 200, so an error after that
    point can only be an event in the body -- which a naive client will happily
    render as if it were an answer. Everything that can be checked up front is.
    """
    try:
        chat_bot = _chat_for(app)
        if not (payload.question or "").strip():
            raise ValueError("A question is required.")
        usage_module.check_budget(app.conn, app.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except usage_module.BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    def events():
        try:
            for event in chat_bot.stream(payload.question, history=payload.history):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 - the stream is already open
            logger.exception("chat stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Nginx buffers proxied responses by default, which holds the whole
            # stream until it finishes and turns streaming back into waiting.
            "X-Accel-Buffering": "no",
        },
    )


@api.get("/usage")
def usage_summary(app: StudyLink = Depends(current_app)) -> dict:
    """What this account has spent on AI, and what is left."""
    spend = usage_module.spend_since(app.conn, app.user_id)
    limit = usage_module.budget_micros()
    return {
        **spend.as_dict(),
        "budget_usd": round(limit / 1_000_000, 6) if limit else None,
        "remaining_usd": round(
            usage_module.remaining_micros(app.conn, app.user_id) / 1_000_000, 6
        ) if limit else None,
    }


@api.get("/canvas")
def canvas_status(app: StudyLink = Depends(current_app)) -> dict:
    """Whether this account has Canvas connected. Never returns the token."""
    connection = app.canvas_connection()
    if connection is None:
        return {"connected": False}
    return {"connected": True, **connection.as_dict()}


@api.post("/canvas/connect")
def canvas_connect(
    payload: CanvasConnectIn,
    app: StudyLink = Depends(current_app),
) -> dict:
    """Store this account's Canvas credentials, encrypted.

    The response echoes the URL and never the token -- there is no read path for
    a stored token outside the syncer, deliberately.
    """
    try:
        connection = credentials.connect_canvas(
            app.conn, app.user_id, payload.api_url, payload.api_token
        )
    except credentials.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connected": True, **connection.as_dict()}


@api.delete("/canvas")
def canvas_disconnect(app: StudyLink = Depends(current_app)) -> dict:
    removed = credentials.disconnect(app.conn, app.user_id)
    return {"connected": False, "removed": removed}


@api.post("/sync", status_code=202)
def sync(app: StudyLink = Depends(current_app)) -> dict:
    """Queue a Canvas sync and return immediately.

    202, not 200: the work has been accepted, not done. Canvas sync is a
    paginated series of calls to someone else's server, and holding a browser
    connection open for it fails at the proxy timeout in a way the user cannot
    act on. Poll /jobs/{id} for the outcome.
    """
    if app.canvas_connection() is None and not (
        app.user.auth_source == "local" and app.settings.canvas_configured
    ):
        # Worth failing here rather than queueing work that cannot succeed --
        # a job that fails 30 seconds later is a worse way to learn this.
        raise HTTPException(
            status_code=400,
            detail="Canvas is not connected for this account. Connect it at "
                   "POST /canvas/connect.",
        )
    return jobs_module.enqueue(app.conn, app.user_id, jobs_module.KIND_CANVAS_SYNC).as_dict()


@api.post("/reindex", status_code=202)
def reindex(app: StudyLink = Depends(current_app)) -> dict:
    """Queue a reindex. Poll /jobs/{id} for the outcome."""
    return jobs_module.enqueue(app.conn, app.user_id, jobs_module.KIND_REINDEX).as_dict()


@api.get("/jobs")
def list_jobs(limit: int = 20, app: StudyLink = Depends(current_app)) -> list[dict]:
    """This account's recent jobs, newest first."""
    return [
        job.as_dict()
        for job in jobs_module.list_for_user(app.conn, app.user_id, limit=min(limit, 100))
    ]


@api.get("/jobs/{job_id}")
def get_job(job_id: int, app: StudyLink = Depends(current_app)) -> dict:
    """One job. Somebody else's job is a 404, not a 403."""
    job = jobs_module.get(app.conn, job_id, app.user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return job.as_dict()


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


# ---------------------------------------------------------------------- cards


@api.post("/decks", status_code=201)
def create_deck(payload: DeckIn, app: StudyLink = Depends(current_app)) -> dict:
    """Generate a deck of flashcards from one note.

    `rejected` is part of the response on purpose. Cards that could not be
    traced to a sentence in the note are dropped, and saying how many were
    dropped is a measurable groundedness signal rather than a promise.
    """
    try:
        return app.make_deck_from_note(
            payload.note_id, count=payload.count, title=payload.title
        )
    except cards_module.GenerationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except cards_module.CardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except usage_module.BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@api.get("/decks")
def list_decks(app: StudyLink = Depends(current_app)) -> list[dict]:
    return app.list_decks()


@api.get("/decks/{deck_id}")
def get_deck(deck_id: int, app: StudyLink = Depends(current_app)) -> dict:
    deck = app.get_deck(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="not found")
    return {**deck, **app.deck_stats(deck_id), "items": app.list_cards(deck_id=deck_id)}


@api.delete("/decks/{deck_id}", status_code=204)
def delete_deck(deck_id: int, app: StudyLink = Depends(current_app)) -> Response:
    app.delete_deck(deck_id)
    return Response(status_code=204)


@api.get("/decks/{deck_id}/study")
def study_queue(
    deck_id: int, limit: int = 20, app: StudyLink = Depends(current_app)
) -> list[dict]:
    """The cards due now, soonest first.

    Answers are included: this is a study session, and hiding the back until
    the student flips it is the client's job. Round-tripping for each answer
    would put a network hop in the middle of the one interaction that has to
    feel immediate.
    """
    return app.list_cards(deck_id=deck_id, due_only=True)[: max(1, min(limit, 100))]


@api.post("/cards/{card_id}/review")
def review_card(
    card_id: int, payload: ReviewIn, app: StudyLink = Depends(current_app)
) -> dict:
    try:
        return app.review_card(card_id, payload.grade)
    except cards_module.CardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.get("/decks/{deck_id}/test")
def practice_test(
    deck_id: int,
    kind: str = "multiple_choice",
    length: int = 10,
    app: StudyLink = Depends(current_app),
) -> dict:
    """A practice test built from cards that already exist.

    No model call: the wrong answers are other answers from the same deck,
    which is free and produces better distractors than generating them, because
    options drawn from the same lecture are genuinely confusable.
    """
    if kind not in {"multiple_choice", "written"}:
        raise HTTPException(status_code=422, detail="kind must be multiple_choice or written")
    try:
        questions = app.build_test(deck_id, kind=kind, length=length)
    except cards_module.CardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"deck_id": deck_id, "questions": [q.as_dict() for q in questions]}


@api.post("/cards/check")
def check_written(
    payload: WrittenAnswerIn, app: StudyLink = Depends(current_app)
) -> dict:
    """Grade a typed answer against the stored one.

    Pure string comparison over two values the caller already has, so it needs
    no data -- but it still requires a session, because "every endpoint is
    authenticated" is a rule worth more than the one exception it would buy.

    "close" is a real verdict rather than a hedge: exact match on free text is
    brutal, and deciding a paraphrase is right needs a model call per answer.
    The student is shown the real answer and makes the call.
    """
    return {
        "verdict": cards_module.grade_written(payload.given, payload.expected),
        "expected": payload.expected,
    }


@api.get("/progress")
def progress(
    deck_id: Optional[int] = None,
    days: int = 30,
    app: StudyLink = Depends(current_app),
) -> dict:
    """How the studying is going: mastery, accuracy, streak, recent activity."""
    return app.progress(deck_id=deck_id, days=min(max(days, 1), 365))


@api.get("/progress/weak")
def weak_cards(
    deck_id: Optional[int] = None,
    limit: int = 10,
    app: StudyLink = Depends(current_app),
) -> list[dict]:
    """Cards worth revisiting, worst first.

    Ranked by a smoothed success rate rather than the raw one: a single wrong
    answer out of one attempt reads as 0% and would otherwise outrank a card
    missed eight times out of twenty, which is the one actually going badly.
    """
    return app.needs_practice(deck_id=deck_id, limit=min(max(limit, 1), 100))


@api.get("/notes/{note_id}/cards")
def note_cards(note_id: int, app: StudyLink = Depends(current_app)) -> dict:
    """The cards a note currently declares in its own text.

    A preview, parsed rather than read back from storage, so the editor can
    show what will exist after a save without having to save first.
    """
    note = app.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"note_id": note_id, "cards": outline.to_cards(note.body)}


@api.post("/outline/preview")
def preview_outline(
    payload: OutlinePreviewIn, app: StudyLink = Depends(current_app)
) -> dict:
    """Parse text and report the cards it declares, without storing anything.

    Lets the editor show a live count as someone types, which is what teaches
    the syntax. Reads nothing and writes nothing, and still takes a session --
    same reasoning as /cards/check: the value of "every endpoint is
    authenticated" is that it has no exceptions to remember.
    """
    return {"cards": outline.to_cards(payload.text)}


@api.get("/evaluation")
def evaluation(app: StudyLink = Depends(current_app)) -> dict:
    return app.evaluate().as_dict()
