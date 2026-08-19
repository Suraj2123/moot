"""Per-user Canvas credentials.

The token goes in encrypted and comes back only when something is about to call
Canvas with it. Everything else -- the API, the UI, the logs -- deals in
`CanvasConnection`, which deliberately has no field that could hold one.

The one rule worth stating: **there is no function here that returns a token to
a caller who did not prove which user they are.** Every read takes a user id and
the vault refuses to decrypt under a different one, so a bug that passes the
wrong id fails loudly instead of handing over the wrong account's Canvas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Connection, delete, select, update

from . import vault
from .db import transaction
from .schema import canvas_credentials

PURPOSE = "canvas"


class CredentialError(RuntimeError):
    """Something is wrong with the stored credentials, safe to show a user."""


@dataclass(frozen=True)
class CanvasConnection:
    """What Canvas connection status looks like to everyone except the syncer.

    No token field, by construction. A dataclass that cannot hold a secret
    cannot leak one through a log line or a JSON response.
    """

    api_url: str
    connected_at: datetime
    updated_at: datetime
    verified_at: Optional[datetime]

    def as_dict(self) -> dict:
        return {
            "api_url": self.api_url,
            "connected_at": self.connected_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalise_url(api_url: str) -> str:
    url = (api_url or "").strip().rstrip("/")
    if not url:
        raise CredentialError("A Canvas URL is required.")
    if not url.startswith(("http://", "https://")):
        raise CredentialError("Canvas URL must start with http:// or https://")
    return url


def connect_canvas(
    conn: Connection, user_id: int, api_url: str, api_token: str
) -> CanvasConnection:
    """Store (or replace) one user's Canvas credentials, encrypted.

    Replacing rather than adding: one Canvas account per user keeps sync
    idempotent on (course_id, canvas_id), which two accounts would break.
    """
    url = normalise_url(api_url)
    if not (api_token or "").strip():
        raise CredentialError("A Canvas access token is required.")

    try:
        encrypted = vault.encrypt(api_token.strip(), user_id, PURPOSE)
    except vault.VaultNotConfigured as exc:
        # Deliberately not degraded to plaintext storage.
        raise CredentialError(
            "This server cannot store Canvas tokens: no encryption key is "
            "configured. Set STUDYLINK_SECRET_KEY."
        ) from exc
    except vault.VaultError as exc:
        # A key that is present but malformed -- wrong base64 alphabet,
        # stripped padding, wrong length. Without this branch the failure
        # reaches the user as a 500 and a stack trace, which reads as "the
        # app is broken" rather than "this server is misconfigured". The
        # message says which, and says nothing about the key itself.
        raise CredentialError(
            "This server cannot store Canvas tokens: its encryption key is "
            "not usable. This is a server configuration problem, not "
            "something you can fix from here."
        ) from exc

    now = _now()
    with transaction(conn):
        existing = conn.execute(
            select(canvas_credentials.c.id).where(
                canvas_credentials.c.user_id == int(user_id)
            )
        ).first()

        values = {
            "api_url": url,
            "encrypted_token": encrypted,
            "key_version": vault.key_version(encrypted),
            "updated_at": now,
            # A replaced token is unverified again: the old one working says
            # nothing about the new one.
            "verified_at": None,
        }
        if existing:
            conn.execute(
                update(canvas_credentials)
                .where(canvas_credentials.c.user_id == int(user_id))
                .values(**values)
            )
        else:
            conn.execute(
                canvas_credentials.insert().values(
                    user_id=int(user_id), created_at=now, **values
                )
            )

    return get_connection(conn, user_id)


def get_connection(conn: Connection, user_id: int) -> Optional[CanvasConnection]:
    """Connection status for one user. Never includes the token."""
    row = conn.execute(
        select(
            canvas_credentials.c.api_url,
            canvas_credentials.c.created_at,
            canvas_credentials.c.updated_at,
            canvas_credentials.c.verified_at,
        ).where(canvas_credentials.c.user_id == int(user_id))
    ).mappings().first()

    if row is None:
        return None
    return CanvasConnection(
        api_url=row["api_url"],
        connected_at=_as_utc(row["created_at"]),
        updated_at=_as_utc(row["updated_at"]),
        verified_at=_as_utc(row["verified_at"]),
    )


def get_token(conn: Connection, user_id: int) -> Optional[tuple[str, str]]:
    """The (url, token) pair for one user, decrypted. None if not connected.

    The only function that returns a readable token. Callers should hold the
    result for exactly as long as the Canvas call takes and never put it
    anywhere durable.
    """
    row = conn.execute(
        select(
            canvas_credentials.c.api_url,
            canvas_credentials.c.encrypted_token,
        ).where(canvas_credentials.c.user_id == int(user_id))
    ).mappings().first()

    if row is None:
        return None

    try:
        token = vault.decrypt(row["encrypted_token"], user_id, PURPOSE)
    except vault.VaultError as exc:
        # A stored token that will not decrypt is a key problem or tampering,
        # and both should be loud rather than looking like "not connected".
        raise CredentialError(
            "Stored Canvas credentials could not be decrypted. If the "
            "encryption key was rotated, the old key must stay loaded until "
            "credentials are re-encrypted."
        ) from exc

    return row["api_url"], token


def mark_verified(conn: Connection, user_id: int) -> None:
    """Record that these credentials just worked."""
    with transaction(conn):
        conn.execute(
            update(canvas_credentials)
            .where(canvas_credentials.c.user_id == int(user_id))
            .values(verified_at=_now())
        )


def disconnect(conn: Connection, user_id: int) -> bool:
    """Forget one user's Canvas credentials. True if there were any."""
    with transaction(conn):
        result = conn.execute(
            delete(canvas_credentials).where(
                canvas_credentials.c.user_id == int(user_id)
            )
        )
    return result.rowcount > 0
