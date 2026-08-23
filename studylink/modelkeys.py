"""A user's own model API key.

The same shape as `credentials.py`, and for the same reason: a secret that has
to be read back cannot be hashed, so it is encrypted with the vault under an
AAD that binds it to one user and one purpose. A bug that passes the wrong user
id fails to decrypt rather than handing over the wrong person's key.

Why this exists at all. Exactly one feature in this app costs money -- writing
cards from prose -- and the operator paying for it is what forces every free
product eventually to stop being free. A student who supplies their own key
pays Anthropic directly: the operator's bill does not grow with their usage,
and the monthly cap that exists to protect the operator has nothing left to
protect, so it does not apply to them.

What this deliberately does not do: return a key to anyone. There is one read
path, it takes a user id, and its only caller is the code about to make a model
call on that user's behalf. The API layer sees `ModelKeyStatus`, which has no
field that could hold a secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Connection, delete, select

from . import vault
from .db import transaction
from .schema import model_credentials

ANTHROPIC = "anthropic"

# Long enough that a truncated paste is caught here rather than by a 401 from
# the provider several seconds later. Not a format check beyond that -- key
# formats change, and refusing a valid key because it does not look like last
# year's is worse than passing it along and reporting what the provider said.
MIN_KEY_CHARS = 20


class ModelKeyError(ValueError):
    """Something is wrong with the supplied key. Safe to show a user."""


@dataclass(frozen=True)
class ModelKeyStatus:
    """What a stored key looks like to everyone except the caller using it.

    No key field, by construction -- a dataclass that cannot hold a secret
    cannot leak one through a log line or a JSON response. `hint` is the last
    four characters, which is enough to recognise a key and useless as one.
    """

    provider: str
    hint: str
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "hint": self.hint,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_key(
    conn: Connection, user_id: int, key: str, provider: str = ANTHROPIC
) -> ModelKeyStatus:
    """Store a user's key, replacing any previous one for that provider."""
    key = (key or "").strip()
    if len(key) < MIN_KEY_CHARS:
        raise ModelKeyError(
            "That does not look like a complete API key. Copy the whole value, "
            "including the prefix."
        )
    if not vault.is_configured():
        raise ModelKeyError(
            "This deployment cannot store secrets: STUDYLINK_SECRET_KEY is not "
            "set. Ask whoever runs it to configure one."
        )

    encrypted = vault.encrypt(key, user_id, provider)
    now = _now()
    existing = conn.execute(
        select(model_credentials.c.id, model_credentials.c.created_at).where(
            model_credentials.c.user_id == user_id,
            model_credentials.c.provider == provider,
        )
    ).first()

    with transaction(conn):
        if existing:
            conn.execute(
                model_credentials.update()
                .where(model_credentials.c.id == existing[0])
                .values(
                    encrypted_key=encrypted,
                    key_version=vault.key_version(encrypted) or "",
                    hint=key[-4:],
                    updated_at=now,
                )
            )
            created = existing[1]
        else:
            conn.execute(
                model_credentials.insert().values(
                    user_id=user_id,
                    provider=provider,
                    encrypted_key=encrypted,
                    key_version=vault.key_version(encrypted) or "",
                    hint=key[-4:],
                    created_at=now,
                    updated_at=now,
                )
            )
            created = now

    return ModelKeyStatus(
        provider=provider, hint=key[-4:], created_at=created, updated_at=now
    )


def get_status(
    conn: Connection, user_id: int, provider: str = ANTHROPIC
) -> Optional[ModelKeyStatus]:
    """Whether a key is stored, and which one -- never the key itself."""
    row = conn.execute(
        select(model_credentials).where(
            model_credentials.c.user_id == user_id,
            model_credentials.c.provider == provider,
        )
    ).mappings().first()
    if not row:
        return None
    return ModelKeyStatus(
        provider=row["provider"],
        hint=row["hint"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_key(
    conn: Connection, user_id: int, provider: str = ANTHROPIC
) -> Optional[str]:
    """The decrypted key, for the one caller about to spend it.

    The only function here that returns a secret. It takes a user id and the
    vault refuses to decrypt under a different one, so this cannot be used to
    read someone else's key even by a caller that wants to.
    """
    row = conn.execute(
        select(model_credentials.c.encrypted_key).where(
            model_credentials.c.user_id == user_id,
            model_credentials.c.provider == provider,
        )
    ).first()
    if not row:
        return None
    try:
        return vault.decrypt(row[0], user_id, provider)
    except vault.VaultError:
        # The key was encrypted under a secret this deployment no longer has --
        # a rotation that lost the old key, or a restored backup. Reporting
        # "no key" sends the user to the settings page to paste a new one,
        # which is the only thing that fixes it.
        return None


def delete_key(conn: Connection, user_id: int, provider: str = ANTHROPIC) -> None:
    with transaction(conn):
        conn.execute(
            delete(model_credentials).where(
                model_credentials.c.user_id == user_id,
                model_credentials.c.provider == provider,
            )
        )
