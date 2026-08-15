"""Ownership errors.

Returning `None` when a row belongs to somebody else conflates two very
different situations: "this does not exist" and "this exists and is not yours".
The first is ordinary. The second means a caller built a query from an id it
should never have had -- a bug, or an attack -- and swallowing it as an empty
result is how an isolation hole survives a test suite.

So the rule is: raise loudly on the inside, mask on the way out. Internally a
cross-user access raises `CrossUserAccessError`, which is loud in logs and in
tests. At the API boundary it is translated to a 404, not a 403, because a 403
confirms the row exists and that is exactly the fact we are protecting.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Connection, select

from .schema import USER_OWNED_TABLES


class StudyLinkError(RuntimeError):
    """Base class for errors this application raises deliberately."""


class NotFoundError(StudyLinkError):
    """The row does not exist for anybody."""

    def __init__(self, table: str, row_id: int) -> None:
        super().__init__(f"{table} {row_id} does not exist")
        self.table = table
        self.row_id = row_id


class CrossUserAccessError(StudyLinkError):
    """The row exists but belongs to a different user.

    Carries both user ids because the pair is what makes the log line useful
    when you are working out which code path produced the foreign id.
    """

    def __init__(self, table: str, row_id: int, owner_id: Optional[int], actor_id: int) -> None:
        super().__init__(
            f"{table} {row_id} belongs to user {owner_id}, not user {actor_id}"
        )
        self.table = table
        self.row_id = row_id
        self.owner_id = owner_id
        self.actor_id = actor_id


def assert_owned(conn: Connection, table: str, row_id: int, user_id: int) -> None:
    """Raise unless `row_id` in `table` belongs to `user_id`.

    Call this at the point a caller-supplied id first enters the system. Once a
    row has been checked, the scoped queries below it cannot widen the scope
    again, so one check at the boundary is enough.
    """
    try:
        target = USER_OWNED_TABLES[table]
    except KeyError:
        raise ValueError(f"{table!r} is not a user-owned table") from None

    row = conn.execute(
        select(target.c.user_id).where(target.c.id == int(row_id))
    ).first()
    if row is None:
        raise NotFoundError(table, int(row_id))

    owner_id = row[0]
    if owner_id != user_id:
        raise CrossUserAccessError(table, int(row_id), owner_id, user_id)
