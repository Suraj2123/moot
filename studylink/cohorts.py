"""Cohort membership, moderation, and the shared read policy.

Owner-only store operations stay owner-only. Pooled readers opt in to this
module's predicates; both the reader and contributor must still be members.
"""
from __future__ import annotations

import secrets
from sqlalchemy import and_, or_, select, insert, update

from .db import transaction
from .errors import NotFoundError, assert_owned
from .schema import cohorts, cohort_memberships as memberships, notes, chunks, users
from . import store


class CohortError(ValueError):
    """A correctable cohort action error, safe to show to its member."""


def require_member(conn, cohort_id: int, user_id: int, *, admin: bool = False):
    row = conn.execute(select(memberships).where(
        memberships.c.cohort_id == cohort_id, memberships.c.user_id == user_id,
        memberships.c.status == "active",
    )).mappings().first()
    if row is None:
        raise NotFoundError("cohorts", cohort_id)
    if admin and row["role"] != "instructor":
        raise CohortError("Only the cohort administrator can do this.")
    return row


def _lock(conn, cohort_id: int):
    # A write lock on SQLite and row lock on Postgres. Serializes membership
    # transitions, invitation rotation, and sharing before permissions are read.
    conn.execute(update(cohorts).where(cohorts.c.id == cohort_id).values(name=cohorts.c.name))


def _text(value: str, label: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit:
        raise CohortError(f"{label} must contain 1–{limit} characters.")
    return value


def set_display_name(conn, user_id: int, display_name: str):
    with transaction(conn):
        conn.execute(update(users).where(users.c.id == user_id).values(
            display_name=_text(display_name, "Display name", 255)))


def create_cohort(conn, user_id: int, name: str, term: str) -> dict:
    with transaction(conn):
        result = conn.execute(insert(cohorts).values(
            name=_text(name, "Cohort name", 120), term=_text(term, "Term", 80),
            invite_code=secrets.token_urlsafe(24), created_at=store._now()))
        cohort_id = int(result.inserted_primary_key[0])
        conn.execute(insert(memberships).values(cohort_id=cohort_id, user_id=user_id,
            role="instructor", status="active", joined_at=store._now()))
    return get_cohort(conn, cohort_id, user_id)


def get_cohort(conn, cohort_id: int, user_id: int) -> dict:
    member = require_member(conn, cohort_id, user_id)
    row = dict(conn.execute(select(cohorts).where(cohorts.c.id == cohort_id)).mappings().one())
    row["created_at"] = store._iso(row["created_at"])
    row["role"] = member["role"]
    if member["role"] != "instructor":
        row.pop("invite_code")
    return row


def list_cohorts(conn, user_id: int) -> list[dict]:
    ids = conn.execute(select(memberships.c.cohort_id).where(
        memberships.c.user_id == user_id, memberships.c.status == "active"
    ).order_by(memberships.c.cohort_id)).scalars().all()
    return [get_cohort(conn, cid, user_id) for cid in ids]


def preview_invite(conn, user_id: int, code: str) -> dict:
    row = conn.execute(select(cohorts.c.id, cohorts.c.name, cohorts.c.term).where(
        cohorts.c.invite_code == code.strip())).mappings().first()
    if row is None:
        raise NotFoundError("invitations", 0)
    removed = conn.execute(select(memberships.c.user_id).where(
        memberships.c.cohort_id == row["id"], memberships.c.user_id == user_id,
        memberships.c.status == "removed")).first()
    if removed:
        raise NotFoundError("invitations", 0)
    return dict(row)


def join_cohort(conn, user_id: int, code: str) -> dict:
    with transaction(conn):
        preview = preview_invite(conn, user_id, code)
        cid = preview["id"]
        _lock(conn, cid)
        # Recheck the code after acquiring the lock (rotation may have won).
        preview_invite(conn, user_id, code)
        statement = store._upsert(conn, memberships).values(cohort_id=cid,
            user_id=user_id, role="member", status="active", joined_at=store._now())
        conn.execute(statement.on_conflict_do_update(
            index_elements=[memberships.c.cohort_id, memberships.c.user_id],
            set_={"status": "active", "joined_at": store._now()},
            where=memberships.c.status == "left"))
    return get_cohort(conn, cid, user_id)


def rotate_invite(conn, cohort_id: int, user_id: int) -> dict:
    with transaction(conn):
        _lock(conn, cohort_id)
        require_member(conn, cohort_id, user_id, admin=True)
        conn.execute(update(cohorts).where(cohorts.c.id == cohort_id).values(invite_code=secrets.token_urlsafe(24)))
    return get_cohort(conn, cohort_id, user_id)


def list_members(conn, cohort_id: int, user_id: int) -> list[dict]:
    require_member(conn, cohort_id, user_id)
    return [dict(r) for r in conn.execute(select(
        memberships.c.user_id, memberships.c.role, users.c.display_name
    ).join(users, users.c.id == memberships.c.user_id).where(
        memberships.c.cohort_id == cohort_id, memberships.c.status == "active"
    ).order_by(memberships.c.joined_at, memberships.c.user_id)).mappings()]


def remove_member(conn, cohort_id: int, user_id: int, target_user_id: int):
    with transaction(conn):
        _lock(conn, cohort_id)
        require_member(conn, cohort_id, user_id, admin=target_user_id != user_id)
        target = require_member(conn, cohort_id, target_user_id)
        if target["role"] == "instructor":
            raise CohortError("Transfer administration before leaving the cohort.")
        conn.execute(update(memberships).where(
            memberships.c.cohort_id == cohort_id, memberships.c.user_id == target_user_id
        ).values(status="left" if user_id == target_user_id else "removed"))
        conn.execute(update(notes).where(notes.c.user_id == target_user_id,
            notes.c.shared_cohort_id == cohort_id).values(visibility="private", shared_cohort_id=None))


def transfer_admin(conn, cohort_id: int, user_id: int, target_user_id: int):
    with transaction(conn):
        _lock(conn, cohort_id)
        require_member(conn, cohort_id, user_id, admin=True)
        require_member(conn, cohort_id, target_user_id)
        if user_id == target_user_id:
            raise CohortError("Choose another member to administer the cohort.")
        conn.execute(update(memberships).where(memberships.c.cohort_id == cohort_id,
            memberships.c.user_id == user_id).values(role="member"))
        conn.execute(update(memberships).where(memberships.c.cohort_id == cohort_id,
            memberships.c.user_id == target_user_id).values(role="instructor"))


def share_note_to_cohort(conn, note_id: int, user_id: int, cohort_id: int | None):
    with transaction(conn):
        assert_owned(conn, "notes", note_id, user_id)
        if cohort_id is not None:
            _lock(conn, cohort_id)
            require_member(conn, cohort_id, user_id)
            owner = store.get_user(conn, user_id)
            if not owner or not (owner["display_name"] or "").strip():
                raise CohortError("Set a display name before sharing a note.")
        conn.execute(update(notes).where(notes.c.id == note_id, notes.c.user_id == user_id).values(
            visibility="private" if cohort_id is None else "cohort", shared_cohort_id=cohort_id))


def moderate_note(conn, cohort_id: int, note_id: int, user_id: int):
    with transaction(conn):
        _lock(conn, cohort_id)
        require_member(conn, cohort_id, user_id, admin=True)
        result = conn.execute(update(notes).where(notes.c.id == note_id,
            notes.c.shared_cohort_id == cohort_id, notes.c.visibility == "cohort"
        ).values(visibility="private", shared_cohort_id=None))
        if not result.rowcount:
            raise NotFoundError("notes", note_id)


def note_scope(user_id: int, cohort_id: int | None):
    """SQL policy, including current reader and contributor membership.

    No optional/global reader: user_id is always required. Own notes are always
    included; a selected cohort adds only explicitly shared notes.
    """
    own = notes.c.user_id == user_id
    if cohort_id is None:
        return own
    readers = memberships.alias("cohort_reader")
    contributors = memberships.alias("cohort_contributor")
    return or_(own, and_(
        notes.c.visibility == "cohort", notes.c.shared_cohort_id == cohort_id,
        select(readers.c.user_id).where(readers.c.cohort_id == cohort_id,
            readers.c.user_id == user_id, readers.c.status == "active").exists(),
        select(contributors.c.user_id).where(contributors.c.cohort_id == cohort_id,
            contributors.c.user_id == notes.c.user_id, contributors.c.status == "active").exists(),
    ))


def chunk_scope(user_id: int, cohort_id: int | None):
    if cohort_id is None:
        return chunks.c.user_id == user_id
    return chunks.c.note_id.in_(select(notes.c.id).where(note_scope(user_id, cohort_id)))


def readable_notes(conn, user_id: int, cohort_id: int | None, note_ids=None, *, shared_only=False):
    if cohort_id is not None:
        require_member(conn, cohort_id, user_id)
    query = store._note_select().add_columns(users.c.display_name).outerjoin(users, users.c.id == notes.c.user_id).where(note_scope(user_id, cohort_id))
    if note_ids is not None:
        query = query.where(notes.c.id.in_(list(note_ids)))
    if shared_only:
        query = query.where(notes.c.visibility == "cohort", notes.c.shared_cohort_id == cohort_id)
    result = []
    for row in conn.execute(query.order_by(notes.c.id)).mappings():
        note = store._note_from_row(row)
        if row["user_id"] != user_id:
            note.contributor_name = row["display_name"] or "A moot contributor"
            # A shared note does not expose its owner's private organization.
            note.course_id = note.folder_id = None
            note.course_name = note.folder_name = ""
        result.append(note)
    return result


def list_cohort_notes(conn, cohort_id: int, user_id: int):
    return readable_notes(conn, user_id, cohort_id, shared_only=True)


def get_shared_note(conn, cohort_id: int, note_id: int, user_id: int):
    found = readable_notes(conn, user_id, cohort_id, [note_id], shared_only=True)
    if not found or found[0].shared_cohort_id != cohort_id or found[0].visibility != "cohort":
        raise NotFoundError("notes", note_id)
    return found[0]


def readable_chunks(conn, user_id: int, cohort_id: int, chunk_ids=None):
    require_member(conn, cohort_id, user_id)
    query = select(chunks).where(chunk_scope(user_id, cohort_id))
    if chunk_ids is not None:
        query = query.where(chunks.c.id.in_(list(chunk_ids)))
    return {r["id"]: store._chunk_from_row(r) for r in conn.execute(query).mappings()}
