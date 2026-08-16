"""Who a request is acting for.

Every read and write below the service layer needs an owner. Passing a bare
`user_id: int` around works, but it is the kind of parameter that gets defaulted
to `None` under deadline pressure and silently reopens the isolation hole this
whole day exists to close.

`UserContext` makes that harder in three ways: it has no default, it carries the
audit fields (how the caller authenticated) that a bare int cannot, and grepping
for `UserContext` finds every ownership-sensitive entry point in one search.
"""

from __future__ import annotations

from sqlalchemy import Connection

from dataclasses import dataclass
from typing import Literal, Optional

from . import store

AuthSource = Literal["local", "password", "session", "apple", "api_key"]


@dataclass(frozen=True)
class UserContext:
    """The identity a unit of work is being performed for."""

    user_id: int
    auth_source: AuthSource = "local"
    email: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError(f"UserContext needs a real user id, got {self.user_id!r}")

    @classmethod
    def local(cls, conn: Connection) -> "UserContext":
        """The single-user context used by the CLI, the seeder, and local dev.

        Day 3 adds `from_apple_token`; until then this is the only constructor
        the app uses, and it is explicit about being a local-only identity.
        """
        return cls(user_id=store.get_or_create_default_user(conn), auth_source="local")

    @classmethod
    def for_user(cls, user_id: int, email: Optional[str] = None) -> "UserContext":
        return cls(user_id=user_id, auth_source="apple", email=email)
