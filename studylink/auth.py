"""Signup, login, and turning a bearer token back into a UserContext.

The rule this module exists to enforce: **a failed login tells the caller
nothing about which part failed.** Not whether the address is registered, not
whether the account has a password, not whether it was the password that was
wrong. All of it is one message and one status code.

That is not paranoia about a login form. An endpoint that answers "no such
account" turns into a membership oracle -- feed it a breach dump and it tells
you which addresses have accounts here, which is worth money and is the first
step of a targeted attack. The same reasoning is why `sessions.resolve` has one
failure mode.

The subtler half is timing. Rejecting an unknown address without hashing
anything returns in microseconds, while rejecting a known address takes the
~400ms scrypt costs. That difference is trivially measurable over the network
and rebuilds the same oracle the identical message was hiding. So the unknown
case hashes against a dummy anyway, and throws the result away.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Connection

from . import passwords, sessions, store
from .context import UserContext


class AuthError(Exception):
    """Authentication failed. The message is safe to return verbatim."""

    status_code = 401


class SignupError(Exception):
    """A signup that cannot proceed. Message is safe to show."""

    status_code = 400


# Hashed once at import so the "unknown address" path has something real to
# verify against. The password is unguessable and belongs to nobody; the point is
# purely that the work happens.
_DUMMY_HASH = passwords.hash_password("not a real password, only here for timing")

GENERIC_LOGIN_FAILURE = "Incorrect email or password."


@dataclass(frozen=True)
class AuthResult:
    token: str
    user: UserContext


def signup(
    conn: Connection,
    email: str,
    password: str,
    display_name: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> AuthResult:
    """Create an account and sign it in.

    Unlike login, this *does* tell you the address is taken. There is no way
    around it -- the account cannot be created, and a signup form that silently
    succeeds without creating anything is worse. The mitigation belongs at the
    endpoint, in the form of a rate limit (commit 8).
    """
    normalised = store.normalise_email(email)
    if not normalised or "@" not in normalised:
        raise SignupError("A valid email address is required.")

    # Raises PasswordError with a message safe to show.
    try:
        passwords.validate(password)
    except passwords.PasswordError as exc:
        raise SignupError(str(exc)) from exc

    if store.get_user_by_email(conn, normalised) is not None:
        raise SignupError("An account with that email already exists.")

    user_id = store.create_user(
        conn,
        email=normalised,
        display_name=display_name or None,
        password_hash=passwords.hash_password(password),
    )

    token, _ = sessions.create(conn, user_id, user_agent=user_agent)
    return AuthResult(
        token=token,
        user=UserContext(user_id=user_id, auth_source="password", email=normalised),
    )


def login(
    conn: Connection,
    email: str,
    password: str,
    user_agent: Optional[str] = None,
) -> AuthResult:
    """Verify credentials and issue a session.

    Every failure raises the same AuthError with the same message. See the module
    docstring for why, including the timing half of it.
    """
    account = store.get_user_by_email(conn, email)

    if account is None or not account.get("password_hash"):
        # Unknown address, or an account that has no password (the seeded local
        # user, or a future provider login). Hash anyway so this path costs the
        # same as a real rejection -- otherwise the response time is the oracle
        # the shared message was there to prevent.
        passwords.verify(password or "", _DUMMY_HASH)
        raise AuthError(GENERIC_LOGIN_FAILURE)

    if not passwords.verify(password, account["password_hash"]):
        raise AuthError(GENERIC_LOGIN_FAILURE)

    # Cost parameters get raised over time; upgrade the stored hash while the
    # plaintext is in hand, which is the only moment it can be done.
    if passwords.needs_rehash(account["password_hash"]):
        store.set_password_hash(conn, account["id"], passwords.hash_password(password))

    token, _ = sessions.create(conn, account["id"], user_agent=user_agent)
    return AuthResult(
        token=token,
        user=UserContext(
            user_id=int(account["id"]),
            auth_source="password",
            email=account["email"],
        ),
    )


def logout(conn: Connection, token: str) -> bool:
    """End one session. True if a live session was actually ended."""
    return sessions.revoke(conn, token)


def context_for_token(conn: Connection, token: str) -> Optional[UserContext]:
    """Resolve a bearer token into the identity it authorises, or None.

    This is the only place a request's identity is established. Everything below
    it takes a UserContext and trusts it, which is why this function has no
    fallback to a default user -- an unauthenticated request has to fail here
    rather than quietly become somebody.
    """
    session = sessions.resolve(conn, token)
    if session is None:
        return None
    return UserContext(
        user_id=session.user_id, auth_source="session", email=session.email
    )


def bearer_token(header_value: Optional[str]) -> Optional[str]:
    """Pull the token out of an `Authorization: Bearer <token>` header."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
