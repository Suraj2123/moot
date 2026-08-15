"""CRUD over courses, assignments, notes, chunks, and eval labels.

Ported from hand-written SQLite SQL to SQLAlchemy Core. The function signatures
are unchanged -- everything above this layer still works with the dataclasses in
`models.py` and never sees a query -- but `conn` is now a SQLAlchemy
`Connection`, so the same code runs against SQLite locally and Postgres in
production.

Two conventions worth knowing when adding to this file:

  * Reads use `.mappings()`, so rows behave like dicts and the `_from_row`
    helpers stay simple.
  * Timestamps are stored as real `DateTime` columns and converted to ISO
    strings on the way out, because the dataclasses and the JSON API both want
    strings. `_iso` handles both engines: Postgres returns `datetime`, and
    SQLAlchemy's SQLite dialect does too.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import Connection, delete, distinct, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import transaction
from .models import Assignment, Chunk, Course, Note
from .schema import (
    assignments,
    chunks,
    courses,
    embeddings,
    eval_labels,
    notes,
    sync_log,
    users,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    """Render a stored timestamp as an ISO string for the dataclasses."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _upsert(conn: Connection, table):
    """Dialect-specific INSERT that supports ON CONFLICT.

    SQLAlchemy's generic `insert()` has no `on_conflict_do_update`; both SQLite
    and Postgres support it but through their own dialect constructs. This picks
    the right one so callers can write a single upsert.
    """
    if conn.dialect.name == "postgresql":
        return pg_insert(table)
    return sqlite_insert(table)


# ---------------------------------------------------------------------------- users

# Until accounts exist (day 3) every row belongs to one local user. Writing a
# real user_id from the start -- rather than leaving it null and backfilling --
# matters because a null user_id is treated as distinct in a UNIQUE index, so it
# would silently break upsert idempotency on re-sync.
DEFAULT_USER_EMAIL = "local@studylink"


def get_or_create_default_user(conn: Connection) -> int:
    row = conn.execute(
        select(users.c.id).where(users.c.email == DEFAULT_USER_EMAIL)
    ).first()
    if row:
        return int(row[0])
    with transaction(conn):
        result = conn.execute(
            insert(users).values(
                email=DEFAULT_USER_EMAIL, display_name="Local user", created_at=_now()
            )
        )
    return int(result.inserted_primary_key[0])


def create_user(
    conn: Connection,
    apple_sub: Optional[str] = None,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
) -> int:
    with transaction(conn):
        result = conn.execute(
            insert(users).values(
                apple_sub=apple_sub,
                email=email,
                display_name=display_name,
                created_at=_now(),
            )
        )
    return int(result.inserted_primary_key[0])


# --------------------------------------------------------------------------- courses


def upsert_course(
    conn: Connection,
    canvas_id: str,
    name: str,
    course_code: str = "",
    user_id: Optional[int] = None,
) -> int:
    user_id = user_id or get_or_create_default_user(conn)
    statement = _upsert(conn, courses).values(
        user_id=user_id,
        canvas_id=str(canvas_id),
        name=name,
        course_code=course_code,
        synced_at=_now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[courses.c.user_id, courses.c.canvas_id],
        set_={
            "name": statement.excluded.name,
            "course_code": statement.excluded.course_code,
            "synced_at": statement.excluded.synced_at,
        },
    )
    with transaction(conn):
        conn.execute(statement)

    row = conn.execute(
        select(courses.c.id).where(
            courses.c.user_id == user_id, courses.c.canvas_id == str(canvas_id)
        )
    ).first()
    return int(row[0])


def _course_from_row(row) -> Course:
    return Course(
        id=row["id"],
        canvas_id=row["canvas_id"],
        name=row["name"],
        course_code=row["course_code"] or "",
        synced_at=_iso(row["synced_at"]),
    )


def list_courses(conn: Connection, user_id: int) -> list[Course]:
    rows = conn.execute(
        select(courses).where(courses.c.user_id == user_id).order_by(courses.c.name)
    ).mappings()
    return [_course_from_row(row) for row in rows]


def get_course(conn: Connection, course_id: int, user_id: int) -> Optional[Course]:
    row = conn.execute(
        select(courses).where(courses.c.id == course_id, courses.c.user_id == user_id)
    ).mappings().first()
    return _course_from_row(row) if row else None


# ----------------------------------------------------------------------- assignments


def upsert_assignment(
    conn: Connection,
    course_id: int,
    canvas_id: str,
    name: str,
    description: str = "",
    due_at: Optional[str] = None,
    points_possible: Optional[float] = None,
    submission_types: str = "",
    html_url: Optional[str] = None,
    user_id: Optional[int] = None,
) -> int:
    user_id = user_id or get_or_create_default_user(conn)
    statement = _upsert(conn, assignments).values(
        user_id=user_id,
        canvas_id=str(canvas_id),
        course_id=course_id,
        name=name,
        description=description or "",
        due_at=due_at,
        points_possible=points_possible,
        submission_types=submission_types or "",
        html_url=html_url,
        synced_at=_now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[assignments.c.course_id, assignments.c.canvas_id],
        set_={
            "name": statement.excluded.name,
            "description": statement.excluded.description,
            "due_at": statement.excluded.due_at,
            "points_possible": statement.excluded.points_possible,
            "submission_types": statement.excluded.submission_types,
            "html_url": statement.excluded.html_url,
            "synced_at": statement.excluded.synced_at,
        },
    )
    with transaction(conn):
        conn.execute(statement)

    row = conn.execute(
        select(assignments.c.id).where(
            assignments.c.course_id == course_id,
            assignments.c.canvas_id == str(canvas_id),
        )
    ).first()
    return int(row[0])


def _assignment_from_row(row) -> Assignment:
    return Assignment(
        id=row["id"],
        course_id=row["course_id"],
        canvas_id=row["canvas_id"],
        name=row["name"],
        description=row["description"] or "",
        due_at=row["due_at"],
        points_possible=row["points_possible"],
        submission_types=row["submission_types"] or "",
        html_url=row["html_url"],
        course_name=row["course_name"] if "course_name" in row else "",
    )


def _assignment_select():
    return select(assignments, courses.c.name.label("course_name")).join(
        courses, courses.c.id == assignments.c.course_id
    )


def list_assignments(
    conn: Connection, user_id: int, course_id: Optional[int] = None
) -> list[Assignment]:
    statement = _assignment_select().where(assignments.c.user_id == user_id)
    if course_id is not None:
        statement = statement.where(assignments.c.course_id == course_id)
    # NULL due dates sort last so upcoming work leads the dashboard.
    statement = statement.order_by(
        assignments.c.due_at.is_(None), assignments.c.due_at, assignments.c.name
    )
    return [_assignment_from_row(row) for row in conn.execute(statement).mappings()]


def get_assignment(
    conn: Connection, assignment_id: int, user_id: int
) -> Optional[Assignment]:
    row = (
        conn.execute(
            _assignment_select().where(
                assignments.c.id == assignment_id, assignments.c.user_id == user_id
            )
        )
        .mappings()
        .first()
    )
    return _assignment_from_row(row) if row else None


# ----------------------------------------------------------------------------- notes


def _note_select():
    return select(notes, func.coalesce(courses.c.name, "").label("course_name")).join(
        courses, courses.c.id == notes.c.course_id, isouter=True
    )


def _note_from_row(row) -> Note:
    return Note(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        course_id=row["course_id"],
        source_type=row["source_type"],
        created_at=_iso(row["created_at"]) or "",
        course_name=row["course_name"] if "course_name" in row else "",
    )


def create_note(
    conn: Connection,
    title: str,
    body: str,
    course_id: Optional[int] = None,
    source_type: str = "note",
    user_id: Optional[int] = None,
) -> int:
    if source_type not in ("note", "transcript"):
        raise ValueError("source_type must be 'note' or 'transcript'")
    user_id = user_id or get_or_create_default_user(conn)
    with transaction(conn):
        result = conn.execute(
            insert(notes).values(
                user_id=user_id,
                course_id=course_id,
                title=title.strip() or "Untitled note",
                body=body,
                source_type=source_type,
                created_at=_now(),
            )
        )
    return int(result.inserted_primary_key[0])


def update_note(
    conn: Connection, note_id: int, title: str, body: str, user_id: int
) -> None:
    with transaction(conn):
        conn.execute(
            update(notes)
            .where(notes.c.id == note_id, notes.c.user_id == user_id)
            .values(title=title, body=body)
        )


def delete_note(conn: Connection, note_id: int, user_id: int) -> None:
    chunk_ids = [
        row[0]
        for row in conn.execute(
            select(chunks.c.id).where(
                chunks.c.note_id == note_id, chunks.c.user_id == user_id
            )
        )
    ]
    with transaction(conn):
        if chunk_ids:
            conn.execute(
                delete(embeddings).where(
                    embeddings.c.owner_type == "chunk",
                    embeddings.c.owner_id.in_(chunk_ids),
                )
            )
        conn.execute(delete(notes).where(notes.c.id == note_id, notes.c.user_id == user_id))


def list_notes(
    conn: Connection,
    user_id: int,
    course_id: Optional[int] = None,
    search: str = "",
    source_type: Optional[str] = None,
) -> list[Note]:
    statement = _note_select().where(notes.c.user_id == user_id)
    if course_id is not None:
        statement = statement.where(notes.c.course_id == course_id)
    if source_type:
        statement = statement.where(notes.c.source_type == source_type)
    if search.strip():
        needle = f"%{search.strip()}%"
        statement = statement.where(
            notes.c.title.ilike(needle) | notes.c.body.ilike(needle)
        )
    statement = statement.order_by(notes.c.created_at.desc(), notes.c.id.desc())
    return [_note_from_row(row) for row in conn.execute(statement).mappings()]


def get_note(conn: Connection, note_id: int, user_id: int) -> Optional[Note]:
    row = (
        conn.execute(
            _note_select().where(notes.c.id == note_id, notes.c.user_id == user_id)
        )
        .mappings()
        .first()
    )
    return _note_from_row(row) if row else None


def get_notes(
    conn: Connection, note_ids: Iterable[int], user_id: int
) -> dict[int, Note]:
    ids = [int(i) for i in note_ids]
    if not ids:
        return {}
    rows = conn.execute(
        _note_select().where(notes.c.id.in_(ids), notes.c.user_id == user_id)
    ).mappings()
    return {row["id"]: _note_from_row(row) for row in rows}


# ---------------------------------------------------------------------------- chunks


def replace_chunks(
    conn: Connection,
    note_id: int,
    texts: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[int]:
    """Delete a note's existing chunks (and their vectors) and write new ones."""
    old_ids = [
        row[0] for row in conn.execute(select(chunks.c.id).where(chunks.c.note_id == note_id))
    ]
    owner = conn.execute(select(notes.c.user_id).where(notes.c.id == note_id)).first()
    owner_id = owner[0] if owner else None

    with transaction(conn):
        if old_ids:
            conn.execute(
                delete(embeddings).where(
                    embeddings.c.owner_type == "chunk", embeddings.c.owner_id.in_(old_ids)
                )
            )
            conn.execute(delete(chunks).where(chunks.c.note_id == note_id))

        new_ids: list[int] = []
        for ordinal, text in enumerate(texts):
            result = conn.execute(
                insert(chunks).values(
                    user_id=owner_id,
                    note_id=note_id,
                    ordinal=ordinal,
                    text=text,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
            )
            new_ids.append(int(result.inserted_primary_key[0]))
    return new_ids


def _chunk_from_row(row) -> Chunk:
    return Chunk(
        id=row["id"], note_id=row["note_id"], ordinal=row["ordinal"], text=row["text"]
    )


def list_chunks(
    conn: Connection, user_id: int, note_id: Optional[int] = None
) -> list[Chunk]:
    statement = select(chunks).where(chunks.c.user_id == user_id)
    if note_id is not None:
        statement = statement.where(chunks.c.note_id == note_id)
    statement = statement.order_by(chunks.c.note_id, chunks.c.ordinal)
    return [_chunk_from_row(row) for row in conn.execute(statement).mappings()]


def get_chunks(
    conn: Connection, chunk_ids: Iterable[int], user_id: int
) -> dict[int, Chunk]:
    ids = [int(i) for i in chunk_ids]
    if not ids:
        return {}
    rows = conn.execute(
        select(chunks).where(chunks.c.id.in_(ids), chunks.c.user_id == user_id)
    ).mappings()
    return {row["id"]: _chunk_from_row(row) for row in rows}


def chunk_note_map(conn: Connection, user_id: int) -> dict[int, int]:
    """chunk_id -> note_id, for aggregating chunk hits up to notes."""
    rows = conn.execute(
        select(chunks.c.id, chunks.c.note_id).where(chunks.c.user_id == user_id)
    )
    return {row[0]: row[1] for row in rows}


def chunking_params_in_use(
    conn: Connection, user_id: int
) -> Optional[tuple[int, int]]:
    """The (size, overlap) the current chunks were built with, if consistent."""
    rows = conn.execute(
        select(distinct(chunks.c.chunk_size), chunks.c.chunk_overlap)
        .where(chunks.c.user_id == user_id)
        .limit(2)
    ).all()
    if len(rows) == 1:
        return int(rows[0][0]), int(rows[0][1])
    return None


# ----------------------------------------------------------------------- eval labels


def set_label(
    conn: Connection,
    assignment_id: int,
    note_id: int,
    relevant: bool,
    rationale: str = "",
    user_id: Optional[int] = None,
) -> None:
    user_id = user_id or get_or_create_default_user(conn)
    statement = _upsert(conn, eval_labels).values(
        user_id=user_id,
        assignment_id=assignment_id,
        note_id=note_id,
        relevant=1 if relevant else 0,
        rationale=rationale,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[eval_labels.c.assignment_id, eval_labels.c.note_id],
        set_={
            "relevant": statement.excluded.relevant,
            "rationale": statement.excluded.rationale,
        },
    )
    with transaction(conn):
        conn.execute(statement)


def list_labels(conn: Connection, user_id: int) -> list[dict]:
    statement = (
        select(
            eval_labels,
            assignments.c.name.label("assignment_name"),
            notes.c.title.label("note_title"),
        )
        .join(assignments, assignments.c.id == eval_labels.c.assignment_id)
        .join(notes, notes.c.id == eval_labels.c.note_id)
        .where(eval_labels.c.user_id == user_id)
        .order_by(eval_labels.c.assignment_id, eval_labels.c.note_id)
    )
    return [dict(row) for row in conn.execute(statement).mappings()]


def delete_label(
    conn: Connection, assignment_id: int, note_id: int, user_id: int
) -> None:
    with transaction(conn):
        conn.execute(
            delete(eval_labels).where(
                eval_labels.c.assignment_id == assignment_id,
                eval_labels.c.note_id == note_id,
                eval_labels.c.user_id == user_id,
            )
        )


# -------------------------------------------------------------------------- sync log


def record_sync(
    conn: Connection,
    courses_synced: int,
    assignments_synced: int,
    detail: str = "",
    user_id: Optional[int] = None,
) -> None:
    with transaction(conn):
        conn.execute(
            insert(sync_log).values(
                user_id=user_id,
                ran_at=_now(),
                courses=courses_synced,
                assignments=assignments_synced,
                detail=detail,
            )
        )


def last_sync(conn: Connection, user_id: Optional[int] = None) -> Optional[dict]:
    statement = select(sync_log).order_by(sync_log.c.id.desc()).limit(1)
    if user_id is not None:
        statement = statement.where(sync_log.c.user_id == user_id)
    row = conn.execute(statement).mappings().first()
    if not row:
        return None
    record = dict(row)
    record["ran_at"] = _iso(record["ran_at"])
    return record
