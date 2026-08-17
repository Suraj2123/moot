"""Session tokens.

The browser gets an opaque random string. The database gets its SHA-256. Nothing
anywhere stores the token itself, so a leaked backup does not hand the reader a
set of working logins.

Why a plain hash here when passwords get scrypt: a password is low-entropy and
attacker-guessable, so the hash has to be slow enough to make guessing
expensive. A token is 256 bits from `secrets`, so there is nothing to guess --
and since every authenticated request verifies one, a slow KDF would put ~400ms
on every API call for no benefit.

Two more decisions worth naming:

Expiry is absolute, not sliding. A session that renews itself on each use never
ends for an attacker who keeps using it, which is exactly the case where you
wanted it to end. Thirty days is long enough that a normal user rarely notices.

Revocation is a column, not a delete. Keeping the row means "this token was
revoked" and "this token never existed" remain distinguishable, which matters
the first time you are trying to work out what happened to an account.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Connection, delete, insert, select, update

from .db import transaction
from .schema import sessions, users

TOKEN_BYTES = 32
DEFAULT_TTL = timedelta(days=30)


class SessionError(RuntimeError):
    """A token that cannot be used, with a reason not safe to show a user."""


@dataclass(frozen=True)
class Session:
    """A verified session. Only ever built from a token that checked out."""

    id: int
    user_id: int
    email: Optional[str]
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_token() -> str:
    """A URL-safe token with 256 bits of entropy."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex of the token. Deterministic, so it can be looked up by index."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value) -> Optional[datetime]:
    """Normalise what came back from the driver into an aware UTC datetime.

    SQLite hands back naive datetimes and Postgres hands back aware ones. A naive
    value compared against an aware one raises TypeError, so an expiry check that
    works on one backend blows up on the other -- and it blows up inside an auth
    path, where the failure mode is a 500 on every request rather than something
    subtle. Naive values are assumed UTC, which is what both writers store.
    """
    if value is None:
        return None
    if isinstance(value, str):  # SQLite can return ISO strings
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create(
    conn: Connection,
    user_id: int,
    ttl: timedelta = DEFAULT_TTL,
    user_agent: Optional[str] = None,
) -> tuple[str, Session]:
    """Issue a session. Returns the plaintext token and the stored session.

    The token is returned exactly once, here. It is not recoverable afterwards
    because only its hash is kept.
    """
    token = generate_token()
    now = _now()
    expires_at = now + ttl

    with transaction(conn):
        result = conn.execute(
            insert(sessions).values(
                token_hash=hash_token(token),
                user_id=int(user_id),
                created_at=now,
                expires_at=expires_at,
                user_agent=(user_agent or "")[:255] or None,
            )
        )

    return token, Session(
        id=int(result.inserted_primary_key[0]),
        user_id=int(user_id),
        email=None,
        expires_at=expires_at,
    )


def resolve(conn: Connection, token: str) -> Optional[Session]:
    """Turn a token into a session, or None if it is not currently usable.

    One return value for every failure -- unknown, expired, revoked, malformed.
    Distinguishing them for the caller would tell an attacker holding a stolen
    token whether it was ever real, which is information worth having and
    nothing a legitimate client needs.
    """
    if not token or not isinstance(token, str):
        return None

    row = conn.execute(
        select(
            sessions.c.id,
            sessions.c.user_id,
            sessions.c.token_hash,
            sessions.c.expires_at,
            sessions.c.revoked_at,
            users.c.email,
        )
        .join(users, users.c.id == sessions.c.user_id)
        .where(sessions.c.token_hash == hash_token(token))
    ).mappings().first()

    if row is None:
        return None

    # The lookup above already matched on the hash, so this comparison is not
    # load-bearing against a timing attack on the index. It is here so that the
    # final accept/reject is a constant-time comparison regardless of how the
    # row was found.
    if not hmac.compare_digest(row["token_hash"], hash_token(token)):
        return None

    if row["revoked_at"] is not None:
        return None

    expires_at = _as_utc(row["expires_at"])
    if expires_at is None or expires_at <= _now():
        return None

    return Session(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        email=row["email"],
        expires_at=expires_at,
    )


def list_for_user(conn: Connection, user_id: int) -> list[dict]:
    """Every live session for one account, newest first.

    Deliberately does not return token hashes. The point of this endpoint is to
    let someone see and end their own sessions, and a hash is both useless to
    them and one accidental log line away from being a problem. Sessions are
    addressed by their integer id instead.
    """
    rows = conn.execute(
        select(
            sessions.c.id,
            sessions.c.created_at,
            sessions.c.expires_at,
            sessions.c.user_agent,
        )
        .where(
            sessions.c.user_id == int(user_id),
            sessions.c.revoked_at.is_(None),
            sessions.c.expires_at > _now(),
        )
        .order_by(sessions.c.created_at.desc())
    ).mappings().all()

    return [
        {
            "id": int(row["id"]),
            "created_at": _as_utc(row["created_at"]).isoformat(),
            "expires_at": _as_utc(row["expires_at"]).isoformat(),
            "user_agent": row["user_agent"],
        }
        for row in rows
    ]


def revoke_by_id(conn: Connection, session_id: int, user_id: int) -> bool:
    """End one session by id, but only if it belongs to `user_id`.

    The ownership predicate is in the WHERE clause rather than in a check before
    it. A read-then-write leaves a window, and more importantly it is the kind
    of check that gets refactored away -- here, ending someone else's session
    simply matches no rows.
    """
    with transaction(conn):
        result = conn.execute(
            update(sessions)
            .where(
                sessions.c.id == int(session_id),
                sessions.c.user_id == int(user_id),
                sessions.c.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
    return result.rowcount > 0


def revoke_others(conn: Connection, user_id: int, keep_token: str) -> int:
    """End every session for this user except the one making the request.

    "Sign out everywhere else" rather than "everywhere": logging yourself out as
    a side effect of securing your account is a small thing that makes people
    not press the button.
    """
    keep = resolve(conn, keep_token)
    keep_id = keep.id if keep else None

    with transaction(conn):
        statement = update(sessions).where(
            sessions.c.user_id == int(user_id),
            sessions.c.revoked_at.is_(None),
        )
        if keep_id is not None:
            statement = statement.where(sessions.c.id != keep_id)
        result = conn.execute(statement.values(revoked_at=_now()))
    return int(result.rowcount)


def revoke(conn: Connection, token: str) -> bool:
    """End one session. Returns whether a live session was actually ended."""
    if not token:
        return False
    with transaction(conn):
        result = conn.execute(
            update(sessions)
            .where(
                sessions.c.token_hash == hash_token(token),
                sessions.c.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
    return result.rowcount > 0


def revoke_all_for_user(conn: Connection, user_id: int) -> int:
    """End every live session for one account. Returns how many were ended.

    This is the "sign out everywhere" primitive, and it is also what a password
    change should call -- changing a password while old sessions keep working
    does not actually remove whoever you changed it because of.
    """
    with transaction(conn):
        result = conn.execute(
            update(sessions)
            .where(
                sessions.c.user_id == int(user_id),
                sessions.c.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
    return int(result.rowcount)


def purge_expired(conn: Connection, before: Optional[datetime] = None) -> int:
    """Delete sessions that expired before `before` (default: now).

    Expired sessions are already unusable -- `resolve` rejects them -- so this is
    housekeeping rather than a security control. Without it the table grows
    forever, and the row that authenticates every request gets slower to find.
    """
    cutoff = before or _now()
    with transaction(conn):
        result = conn.execute(delete(sessions).where(sessions.c.expires_at < cutoff))
    return int(result.rowcount)
