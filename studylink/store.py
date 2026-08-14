"""CRUD over courses, assignments, notes, chunks, and eval labels.

Everything above this layer (retrieval, agent, UI) works with the dataclasses in
`models.py` and never touches SQL directly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from .db import transaction
from .models import Assignment, Chunk, Course, Note


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- courses


def upsert_course(conn: sqlite3.Connection, canvas_id: str, name: str, course_code: str = "") -> int:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO courses (canvas_id, name, course_code, synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(canvas_id)
            DO UPDATE SET name = excluded.name,
                          course_code = excluded.course_code,
                          synced_at = excluded.synced_at
            """,
            (str(canvas_id), name, course_code, _now()),
        )
    row = conn.execute("SELECT id FROM courses WHERE canvas_id = ?", (str(canvas_id),)).fetchone()
    return int(row["id"])


def list_courses(conn: sqlite3.Connection) -> list[Course]:
    rows = conn.execute("SELECT * FROM courses ORDER BY name").fetchall()
    return [
        Course(
            id=r["id"],
            canvas_id=r["canvas_id"],
            name=r["name"],
            course_code=r["course_code"] or "",
            synced_at=r["synced_at"],
        )
        for r in rows
    ]


def get_course(conn: sqlite3.Connection, course_id: int) -> Optional[Course]:
    row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not row:
        return None
    return Course(
        id=row["id"],
        canvas_id=row["canvas_id"],
        name=row["name"],
        course_code=row["course_code"] or "",
        synced_at=row["synced_at"],
    )


# ----------------------------------------------------------------------- assignments


def upsert_assignment(
    conn: sqlite3.Connection,
    course_id: int,
    canvas_id: str,
    name: str,
    description: str = "",
    due_at: Optional[str] = None,
    points_possible: Optional[float] = None,
    submission_types: str = "",
    html_url: Optional[str] = None,
) -> int:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO assignments
                (canvas_id, course_id, name, description, due_at, points_possible,
                 submission_types, html_url, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id, canvas_id)
            DO UPDATE SET name = excluded.name,
                          description = excluded.description,
                          due_at = excluded.due_at,
                          points_possible = excluded.points_possible,
                          submission_types = excluded.submission_types,
                          html_url = excluded.html_url,
                          synced_at = excluded.synced_at
            """,
            (
                str(canvas_id),
                course_id,
                name,
                description or "",
                due_at,
                points_possible,
                submission_types or "",
                html_url,
                _now(),
            ),
        )
    row = conn.execute(
        "SELECT id FROM assignments WHERE course_id = ? AND canvas_id = ?",
        (course_id, str(canvas_id)),
    ).fetchone()
    return int(row["id"])


def _assignment_from_row(row: sqlite3.Row) -> Assignment:
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
        course_name=row["course_name"] if "course_name" in row.keys() else "",
    )


_ASSIGNMENT_SELECT = """
    SELECT a.*, c.name AS course_name
    FROM assignments a
    JOIN courses c ON c.id = a.course_id
"""


def list_assignments(conn: sqlite3.Connection, course_id: Optional[int] = None) -> list[Assignment]:
    sql = _ASSIGNMENT_SELECT
    params: tuple = ()
    if course_id is not None:
        sql += " WHERE a.course_id = ?"
        params = (course_id,)
    # NULL due dates sort last so upcoming work leads the dashboard.
    sql += " ORDER BY (a.due_at IS NULL), a.due_at, a.name"
    return [_assignment_from_row(r) for r in conn.execute(sql, params).fetchall()]


def get_assignment(conn: sqlite3.Connection, assignment_id: int) -> Optional[Assignment]:
    row = conn.execute(_ASSIGNMENT_SELECT + " WHERE a.id = ?", (assignment_id,)).fetchone()
    return _assignment_from_row(row) if row else None


# ----------------------------------------------------------------------------- notes


_NOTE_SELECT = """
    SELECT n.*, COALESCE(c.name, '') AS course_name
    FROM notes n
    LEFT JOIN courses c ON c.id = n.course_id
"""


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        course_id=row["course_id"],
        source_type=row["source_type"],
        created_at=row["created_at"],
        course_name=row["course_name"] if "course_name" in row.keys() else "",
    )


def create_note(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    course_id: Optional[int] = None,
    source_type: str = "note",
) -> int:
    if source_type not in ("note", "transcript"):
        raise ValueError("source_type must be 'note' or 'transcript'")
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO notes (course_id, title, body, source_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (course_id, title.strip() or "Untitled note", body, source_type, _now()),
        )
    return int(cursor.lastrowid)


def update_note(conn: sqlite3.Connection, note_id: int, title: str, body: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE notes SET title = ?, body = ? WHERE id = ?",
            (title, body, note_id),
        )


def delete_note(conn: sqlite3.Connection, note_id: int) -> None:
    chunk_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE note_id = ?", (note_id,))]
    with transaction(conn):
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            conn.execute(
                f"DELETE FROM embeddings WHERE owner_type = 'chunk' AND owner_id IN ({placeholders})",
                chunk_ids,
            )
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def list_notes(
    conn: sqlite3.Connection,
    course_id: Optional[int] = None,
    search: str = "",
    source_type: Optional[str] = None,
) -> list[Note]:
    sql = _NOTE_SELECT
    clauses: list[str] = []
    params: list = []
    if course_id is not None:
        clauses.append("n.course_id = ?")
        params.append(course_id)
    if source_type:
        clauses.append("n.source_type = ?")
        params.append(source_type)
    if search.strip():
        clauses.append("(n.title LIKE ? OR n.body LIKE ?)")
        needle = f"%{search.strip()}%"
        params.extend([needle, needle])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY n.created_at DESC, n.id DESC"
    return [_note_from_row(r) for r in conn.execute(sql, params).fetchall()]


def get_note(conn: sqlite3.Connection, note_id: int) -> Optional[Note]:
    row = conn.execute(_NOTE_SELECT + " WHERE n.id = ?", (note_id,)).fetchone()
    return _note_from_row(row) if row else None


def get_notes(conn: sqlite3.Connection, note_ids: Iterable[int]) -> dict[int, Note]:
    ids = [int(i) for i in note_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(_NOTE_SELECT + f" WHERE n.id IN ({placeholders})", ids).fetchall()
    return {r["id"]: _note_from_row(r) for r in rows}


# ---------------------------------------------------------------------------- chunks


def replace_chunks(
    conn: sqlite3.Connection,
    note_id: int,
    texts: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[int]:
    """Delete a note's existing chunks (and their vectors) and write new ones."""
    old_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE note_id = ?", (note_id,))]
    with transaction(conn):
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(
                f"DELETE FROM embeddings WHERE owner_type = 'chunk' AND owner_id IN ({placeholders})",
                old_ids,
            )
            conn.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
        new_ids = []
        for ordinal, text in enumerate(texts):
            cursor = conn.execute(
                """
                INSERT INTO chunks (note_id, ordinal, text, chunk_size, chunk_overlap)
                VALUES (?, ?, ?, ?, ?)
                """,
                (note_id, ordinal, text, chunk_size, chunk_overlap),
            )
            new_ids.append(int(cursor.lastrowid))
    return new_ids


def _chunk_from_row(row: sqlite3.Row) -> Chunk:
    return Chunk(id=row["id"], note_id=row["note_id"], ordinal=row["ordinal"], text=row["text"])


def list_chunks(conn: sqlite3.Connection, note_id: Optional[int] = None) -> list[Chunk]:
    sql = "SELECT * FROM chunks"
    params: tuple = ()
    if note_id is not None:
        sql += " WHERE note_id = ?"
        params = (note_id,)
    sql += " ORDER BY note_id, ordinal"
    return [_chunk_from_row(r) for r in conn.execute(sql, params).fetchall()]


def get_chunks(conn: sqlite3.Connection, chunk_ids: Iterable[int]) -> dict[int, Chunk]:
    ids = [int(i) for i in chunk_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM chunks WHERE id IN ({placeholders})", ids).fetchall()
    return {r["id"]: _chunk_from_row(r) for r in rows}


def chunk_note_map(conn: sqlite3.Connection) -> dict[int, int]:
    """chunk_id -> note_id, for aggregating chunk hits up to notes."""
    return {r["id"]: r["note_id"] for r in conn.execute("SELECT id, note_id FROM chunks")}


def chunking_params_in_use(conn: sqlite3.Connection) -> Optional[tuple[int, int]]:
    """The (size, overlap) the current chunks were built with, if consistent."""
    row = conn.execute(
        "SELECT DISTINCT chunk_size, chunk_overlap FROM chunks LIMIT 2"
    ).fetchall()
    if len(row) == 1:
        return int(row[0]["chunk_size"]), int(row[0]["chunk_overlap"])
    return None


# ----------------------------------------------------------------------- eval labels


def set_label(
    conn: sqlite3.Connection,
    assignment_id: int,
    note_id: int,
    relevant: bool,
    rationale: str = "",
) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO eval_labels (assignment_id, note_id, relevant, rationale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(assignment_id, note_id)
            DO UPDATE SET relevant = excluded.relevant, rationale = excluded.rationale
            """,
            (assignment_id, note_id, 1 if relevant else 0, rationale),
        )


def list_labels(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.*, a.name AS assignment_name, n.title AS note_title
        FROM eval_labels l
        JOIN assignments a ON a.id = l.assignment_id
        JOIN notes n ON n.id = l.note_id
        ORDER BY l.assignment_id, l.note_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def delete_label(conn: sqlite3.Connection, assignment_id: int, note_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "DELETE FROM eval_labels WHERE assignment_id = ? AND note_id = ?",
            (assignment_id, note_id),
        )


# -------------------------------------------------------------------------- sync log


def record_sync(conn: sqlite3.Connection, courses: int, assignments: int, detail: str = "") -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO sync_log (ran_at, courses, assignments, detail) VALUES (?, ?, ?, ?)",
            (_now(), courses, assignments, detail),
        )


def last_sync(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
