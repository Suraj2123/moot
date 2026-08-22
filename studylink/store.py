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

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Connection,
    case,
    delete,
    distinct,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import transaction
from .models import Assignment, Chunk, Course, Note
from .schema import (
    assignments,
    card_reviews,
    cards,
    chunks,
    courses,
    decks,
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


def normalise_email(email: str) -> str:
    """The one place an address is turned into its canonical form.

    Lowercased and stripped. Everything that looks up or stores an address goes
    through here, so "the address in the database" and "the address the user
    typed" cannot drift apart -- and the unique index on lower(email) enforces
    the same rule at the storage layer for anything that forgets.
    """
    return (email or "").strip().lower()


def create_user(
    conn: Connection,
    apple_sub: Optional[str] = None,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    password_hash: Optional[str] = None,
) -> int:
    with transaction(conn):
        result = conn.execute(
            insert(users).values(
                apple_sub=apple_sub,
                email=normalise_email(email) if email else None,
                display_name=display_name,
                password_hash=password_hash,
                created_at=_now(),
            )
        )
    return int(result.inserted_primary_key[0])


def get_user_by_email(conn: Connection, email: str) -> Optional[dict]:
    """Look an account up by address, case-insensitively.

    Matches on lower(email) rather than on the raw column so the query uses the
    same rule as the unique index -- and so an address stored before
    normalisation existed is still found.
    """
    normalised = normalise_email(email)
    if not normalised:
        return None
    row = conn.execute(
        select(
            users.c.id,
            users.c.email,
            users.c.display_name,
            users.c.password_hash,
        ).where(func.lower(users.c.email) == normalised)
    ).mappings().first()
    return dict(row) if row else None


def get_user(conn: Connection, user_id: int) -> Optional[dict]:
    row = conn.execute(
        select(
            users.c.id,
            users.c.email,
            users.c.display_name,
            users.c.password_hash,
        ).where(users.c.id == int(user_id))
    ).mappings().first()
    return dict(row) if row else None


def set_password_hash(conn: Connection, user_id: int, password_hash: str) -> None:
    """Used at signup and by login's transparent rehash to a higher cost."""
    with transaction(conn):
        conn.execute(
            update(users)
            .where(users.c.id == int(user_id))
            .values(password_hash=password_hash)
        )


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


def set_note_course(
    conn: Connection, note_id: int, course_id: Optional[int], user_id: int
) -> None:
    """File a note under a course, or under none.

    Separate from `update_note` because None is a real value here -- "no
    course" -- and folding it into a function whose other arguments mean
    "leave alone when None" would make one of the two impossible to express.
    """
    with transaction(conn):
        conn.execute(
            update(notes)
            .where(notes.c.id == note_id, notes.c.user_id == user_id)
            .values(course_id=course_id)
        )


def clear_chunks(conn: Connection, note_id: int, user_id: int) -> int:
    """Drop a note's chunks and their vectors. Returns how many chunks went.

    Called when a note's body changes. The indexer decides what to rebuild by
    asking whether a note's chunks match the *current chunking parameters* --
    it has no way to notice that the text underneath them changed, because
    chunks do not record a hash of what they were cut from. So an edited note
    keeps serving its old chunks to retrieval indefinitely: the note reads
    correctly on screen while matching and citing sentences the student
    deleted.

    Removing the chunks is what makes the note look un-indexed, which is the
    one state the indexer does act on.
    """
    chunk_ids = [
        row[0]
        for row in conn.execute(
            select(chunks.c.id).where(
                chunks.c.note_id == note_id, chunks.c.user_id == user_id
            )
        )
    ]
    if not chunk_ids:
        return 0
    with transaction(conn):
        # embeddings has no foreign key -- it is keyed by (owner_type,
        # owner_id, model) so one table can hold vectors for chunks and
        # assignments alike. Nothing cascades here; it has to be explicit or
        # the rows outlive their chunks forever.
        conn.execute(
            delete(embeddings).where(
                embeddings.c.owner_type == "chunk",
                embeddings.c.owner_id.in_(chunk_ids),
            )
        )
        conn.execute(
            delete(chunks).where(
                chunks.c.note_id == note_id, chunks.c.user_id == user_id
            )
        )
    return len(chunk_ids)


def note_index_status(
    conn: Connection,
    user_id: int,
    model: str,
    chunk_size: int,
    chunk_overlap: int,
) -> dict[int, str]:
    """Per note: is it searchable right now, or waiting on the indexer?

    One query for the whole list rather than one per note -- this feeds the
    notes list, and a per-note version would be a query per row.

    "indexed" means every chunk of this note was cut with the current
    parameters and has a vector for the current model. Anything else is
    "stale": either it has never been chunked, or the configuration moved
    under it, or embedding has not caught up. The distinction the student
    needs is only "can this be found yet", so there are two states, not five.
    """
    chunk_rows = conn.execute(
        select(
            chunks.c.note_id,
            func.count(chunks.c.id),
            func.sum(
                case(
                    (
                        (chunks.c.chunk_size == chunk_size)
                        & (chunks.c.chunk_overlap == chunk_overlap),
                        1,
                    ),
                    else_=0,
                )
            ),
        )
        .where(chunks.c.user_id == user_id)
        .group_by(chunks.c.note_id)
    ).all()

    embedded = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            select(chunks.c.note_id, func.count(embeddings.c.owner_id))
            .select_from(
                chunks.join(
                    embeddings,
                    (embeddings.c.owner_type == "chunk")
                    & (embeddings.c.owner_id == chunks.c.id)
                    & (embeddings.c.model == model),
                )
            )
            .where(chunks.c.user_id == user_id)
            .group_by(chunks.c.note_id)
        )
    }

    status: dict[int, str] = {}
    for note_id, total, current in chunk_rows:
        note_id, total, current = int(note_id), int(total), int(current or 0)
        status[note_id] = (
            "indexed"
            if total and current == total and embedded.get(note_id, 0) == total
            else "stale"
        )
    return status


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


# ------------------------------------------------------------------------ cards


def create_deck(
    conn: Connection, user_id: int, title: str, course_id: Optional[int] = None
) -> int:
    with transaction(conn):
        result = conn.execute(
            insert(decks).values(
                user_id=user_id, title=title, course_id=course_id, created_at=_now()
            )
        )
    return int(result.inserted_primary_key[0])


def add_cards(conn: Connection, user_id: int, deck_id: int, drafts: Iterable) -> list[int]:
    """Write generated cards. New cards are due immediately -- an unstudied
    card is exactly the one that should come up first."""
    ids = []
    now = _now()
    with transaction(conn):
        for draft in drafts:
            result = conn.execute(
                insert(cards).values(
                    user_id=user_id,
                    deck_id=deck_id,
                    note_id=getattr(draft, "note_id", None),
                    front=draft.front,
                    back=draft.back,
                    evidence=getattr(draft, "evidence", "") or "",
                    created_at=now,
                    due_at=now,
                    interval_days=0,
                    ease=250,
                    reviews=0,
                    lapses=0,
                )
            )
            ids.append(int(result.inserted_primary_key[0]))
    return ids


def _deck_from_row(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "course_id": row["course_id"],
        "created_at": _iso(row["created_at"]),
    }


def list_decks(conn: Connection, user_id: int) -> list[dict]:
    """Decks with their counts, in one query.

    `due` drives the only number on the screen a student acts on, so computing
    it per deck in Python would mean a query per row on the page that matters
    most.
    """
    rows = conn.execute(
        select(
            decks.c.id,
            decks.c.title,
            decks.c.course_id,
            decks.c.created_at,
            func.count(cards.c.id).label("total"),
            func.sum(
                case(((cards.c.due_at.is_(None)) | (cards.c.due_at <= _now()), 1), else_=0)
            ).label("due"),
        )
        .select_from(decks.outerjoin(cards, cards.c.deck_id == decks.c.id))
        .where(decks.c.user_id == user_id)
        .group_by(decks.c.id, decks.c.title, decks.c.course_id, decks.c.created_at)
        .order_by(decks.c.created_at.desc())
    ).mappings()
    return [
        {**_deck_from_row(row), "cards": int(row["total"]), "due": int(row["due"] or 0)}
        for row in rows
    ]


def get_deck(conn: Connection, deck_id: int, user_id: int) -> Optional[dict]:
    row = conn.execute(
        select(decks).where(decks.c.id == deck_id, decks.c.user_id == user_id)
    ).mappings().first()
    return _deck_from_row(row) if row else None


def _card_from_row(row) -> dict:
    return {
        "id": row["id"],
        "deck_id": row["deck_id"],
        "note_id": row["note_id"],
        "front": row["front"],
        "back": row["back"],
        "evidence": row["evidence"] or "",
        "due_at": _iso(row["due_at"]),
        "interval_days": row["interval_days"],
        "ease": row["ease"],
        "reviews": row["reviews"],
        "lapses": row["lapses"],
    }


def list_cards(
    conn: Connection, user_id: int, deck_id: Optional[int] = None, due_only: bool = False
) -> list[dict]:
    statement = select(cards).where(cards.c.user_id == user_id)
    if deck_id is not None:
        statement = statement.where(cards.c.deck_id == deck_id)
    if due_only:
        statement = statement.where(
            (cards.c.due_at.is_(None)) | (cards.c.due_at <= _now())
        )
    rows = conn.execute(statement.order_by(cards.c.due_at, cards.c.id)).mappings()
    return [_card_from_row(row) for row in rows]


def get_card(conn: Connection, card_id: int, user_id: int) -> Optional[dict]:
    row = conn.execute(
        select(cards).where(cards.c.id == card_id, cards.c.user_id == user_id)
    ).mappings().first()
    return _card_from_row(row) if row else None


def record_review(
    conn: Connection,
    user_id: int,
    card_id: int,
    grade: int,
    interval_days: int,
    ease: int,
    due_at: datetime,
    lapsed: bool,
) -> None:
    """Update the card's schedule and append to its history, together.

    One transaction because the two must not diverge: a card whose state says
    it was reviewed and whose history does not is a card the scheduler can
    never be re-fitted against.
    """
    with transaction(conn):
        conn.execute(
            update(cards)
            .where(cards.c.id == card_id, cards.c.user_id == user_id)
            .values(
                interval_days=interval_days,
                ease=ease,
                due_at=due_at,
                reviews=cards.c.reviews + 1,
                lapses=cards.c.lapses + (1 if lapsed else 0),
            )
        )
        conn.execute(
            insert(card_reviews).values(
                user_id=user_id, card_id=card_id, grade=grade, reviewed_at=_now()
            )
        )


def delete_deck(conn: Connection, deck_id: int, user_id: int) -> None:
    with transaction(conn):
        conn.execute(delete(decks).where(decks.c.id == deck_id, decks.c.user_id == user_id))


def deck_stats(conn: Connection, user_id: int, deck_id: int) -> dict:
    """What the student has actually done with this deck."""
    row = conn.execute(
        select(
            func.count(cards.c.id),
            func.sum(case((cards.c.reviews > 0, 1), else_=0)),
            func.sum(case(((cards.c.due_at.is_(None)) | (cards.c.due_at <= _now()), 1), else_=0)),
        ).where(cards.c.user_id == user_id, cards.c.deck_id == deck_id)
    ).first()
    total, studied, due = (int(v or 0) for v in row)
    return {"cards": total, "studied": studied, "due": due}


def sync_note_cards(
    conn: Connection, user_id: int, note_id: int, parsed: list[dict], title: str
) -> dict:
    """Bring a note's own deck in line with the cards its text declares.

    Called on every save of a note that contains card syntax. The whole
    difficulty is what to do with cards that already exist: rebuilding the deck
    from scratch would be trivial and would throw away the review history,
    which is the only part of a flashcard that took weeks to produce.

    So cards are matched on `source_key` -- the question as asked:

      * still present  -> the answer and evidence are updated in place, and the
                          schedule is left completely alone.
      * newly present  -> inserted, due immediately.
      * gone from text -> deleted. A student who removes a line from their
                          notes is saying they no longer want to be asked it,
                          and leaving orphans behind would mean cards that
                          cannot be found or edited from anywhere.

    Only ever touches the deck with source='note' for this note. Generated
    decks are authored once and are not the sync's business.
    """
    now = _now()
    row = conn.execute(
        select(decks.c.id).where(
            decks.c.user_id == user_id,
            decks.c.note_id == note_id,
            decks.c.source == "note",
        )
    ).first()

    if not parsed:
        # The note declares nothing. Drop an empty shell rather than leaving a
        # deck that can never be studied cluttering the list.
        if row:
            with transaction(conn):
                conn.execute(delete(decks).where(decks.c.id == int(row[0])))
        return {"deck_id": None, "added": 0, "updated": 0, "removed": 0}

    if row:
        deck_id = int(row[0])
        with transaction(conn):
            conn.execute(
                update(decks).where(decks.c.id == deck_id).values(title=title)
            )
    else:
        with transaction(conn):
            result = conn.execute(
                insert(decks).values(
                    user_id=user_id, title=title, note_id=note_id,
                    source="note", created_at=now,
                )
            )
        deck_id = int(result.inserted_primary_key[0])

    existing = {
        row["source_key"]: row["id"]
        for row in conn.execute(
            select(cards.c.id, cards.c.source_key).where(
                cards.c.deck_id == deck_id, cards.c.user_id == user_id
            )
        ).mappings()
        if row["source_key"]
    }

    added = updated = 0
    seen: set[str] = set()
    with transaction(conn):
        for item in parsed:
            key = item["source_key"]
            if key in seen:
                # Two identical questions in one note. The second is not a
                # separate card, and inserting it would make a duplicate that
                # can never be told apart in review.
                continue
            seen.add(key)

            if key in existing:
                conn.execute(
                    update(cards)
                    .where(cards.c.id == existing[key])
                    .values(
                        front=item["front"],
                        back=item["back"],
                        evidence=item.get("evidence", ""),
                    )
                )
                updated += 1
            else:
                conn.execute(
                    insert(cards).values(
                        user_id=user_id, deck_id=deck_id, note_id=note_id,
                        front=item["front"], back=item["back"],
                        evidence=item.get("evidence", ""),
                        source_key=key, created_at=now, due_at=now,
                        interval_days=0, ease=250, reviews=0, lapses=0,
                    )
                )
                added += 1

        stale = [card_id for key, card_id in existing.items() if key not in seen]
        if stale:
            conn.execute(delete(cards).where(cards.c.id.in_(stale)))

    return {
        "deck_id": deck_id, "added": added,
        "updated": updated, "removed": len(stale),
    }


# ------------------------------------------------------------------ progress


def card_performance(
    conn: Connection, user_id: int, deck_id: Optional[int] = None
) -> list[dict]:
    """Per card: how often it has been recalled, and how often it has not.

    Accuracy comes from `card_reviews` rather than from the card's own
    counters, because the counters describe the schedule and this describes the
    student. A card answered wrong four times and right once is at 20% however
    comfortable its current interval looks.
    """
    correct = func.sum(case((card_reviews.c.grade >= 2, 1), else_=0))
    statement = (
        select(
            cards.c.id,
            cards.c.deck_id,
            cards.c.front,
            cards.c.back,
            cards.c.lapses,
            cards.c.interval_days,
            cards.c.due_at,
            func.count(card_reviews.c.id).label("attempts"),
            correct.label("correct"),
        )
        .select_from(cards.outerjoin(card_reviews, card_reviews.c.card_id == cards.c.id))
        .where(cards.c.user_id == user_id)
        .group_by(
            cards.c.id, cards.c.deck_id, cards.c.front, cards.c.back,
            cards.c.lapses, cards.c.interval_days, cards.c.due_at,
        )
    )
    if deck_id is not None:
        statement = statement.where(cards.c.deck_id == deck_id)

    rows = []
    for row in conn.execute(statement).mappings():
        attempts = int(row["attempts"] or 0)
        right = int(row["correct"] or 0)
        rows.append({
            "id": row["id"],
            "deck_id": row["deck_id"],
            "front": row["front"],
            "back": row["back"],
            "attempts": attempts,
            "correct": right,
            "accuracy": round(right / attempts, 3) if attempts else None,
            "lapses": int(row["lapses"] or 0),
            "interval_days": int(row["interval_days"] or 0),
            "due_at": _iso(row["due_at"]),
        })
    return rows


def review_activity(conn: Connection, user_id: int, days: int = 30) -> list[dict]:
    """Reviews per day, for a streak and a trend.

    Grouped in SQL on a date expression rather than in Python over every row,
    because this is the query that grows fastest -- a year of daily study is
    tens of thousands of rows and the answer is 365 numbers.
    """
    since = _now() - timedelta(days=days)
    day = func.date(card_reviews.c.reviewed_at)
    rows = conn.execute(
        select(
            day.label("day"),
            func.count(card_reviews.c.id).label("reviews"),
            func.sum(case((card_reviews.c.grade >= 2, 1), else_=0)).label("correct"),
        )
        .where(card_reviews.c.user_id == user_id, card_reviews.c.reviewed_at >= since)
        .group_by(day)
        .order_by(day)
    ).mappings()
    return [
        {
            "day": str(row["day"]),
            "reviews": int(row["reviews"]),
            "correct": int(row["correct"] or 0),
        }
        for row in rows
    ]
