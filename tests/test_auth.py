"""Signup and login.

The centre of gravity here is that a failed login is indistinguishable from any
other failed login -- same message, and same cost. An endpoint that answers "no
such account" is a membership oracle: feed it a breach dump and it tells you
which addresses have accounts here. An endpoint that answers the same message
but *faster* for unknown addresses is the same oracle wearing a hat.
"""

from __future__ import annotations

import time

import pytest

from studylink import auth, passwords, sessions, store


def _hash_with_cost(password: str, n: int) -> str:
    """A genuinely valid hash at a lower cost, standing in for an older record.

    Rewriting the parameter string of a current hash does not work: verify()
    would re-derive with the new n and get a different digest, so the hash fails
    to verify for a reason unrelated to what the test is about.
    """
    import base64
    import hashlib
    import secrets

    salt = secrets.token_bytes(passwords.SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=passwords.SCRYPT_R,
        p=passwords.SCRYPT_P,
        dklen=passwords.KEY_BYTES,
        maxmem=128 * n * passwords.SCRYPT_R * 2,
    )
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return f"scrypt$n={n},r={passwords.SCRYPT_R},p={passwords.SCRYPT_P}${b64(salt)}${b64(derived)}"


def test_signup_creates_an_account_and_signs_it_in(conn):
    result = auth.signup(conn, "Alice@School.edu", "correct horse battery")

    assert result.user.email == "alice@school.edu"
    assert result.user.auth_source == "password"
    assert sessions.resolve(conn, result.token).user_id == result.user.user_id


def test_signup_normalises_the_address(conn):
    auth.signup(conn, "  Alice@School.EDU  ", "correct horse battery")
    assert store.get_user_by_email(conn, "alice@school.edu") is not None


def test_signup_rejects_a_duplicate_address_in_any_case(conn):
    auth.signup(conn, "alice@school.edu", "correct horse battery")
    for spelling in ["alice@school.edu", "ALICE@SCHOOL.EDU", " Alice@School.edu "]:
        with pytest.raises(auth.SignupError):
            auth.signup(conn, spelling, "another password entirely")


def test_signup_rejects_bad_input(conn):
    for email, password in [
        ("", "correct horse battery"),
        ("not-an-email", "correct horse battery"),
        ("alice@school.edu", "short"),
        ("alice@school.edu", ""),
    ]:
        with pytest.raises(auth.SignupError):
            auth.signup(conn, email, password)


def test_a_failed_signup_creates_nothing(conn):
    with pytest.raises(auth.SignupError):
        auth.signup(conn, "alice@school.edu", "short")
    assert store.get_user_by_email(conn, "alice@school.edu") is None


def test_login_with_the_right_password(conn):
    auth.signup(conn, "alice@school.edu", "correct horse battery")
    result = auth.login(conn, "Alice@School.edu", "correct horse battery")

    assert result.user.email == "alice@school.edu"
    assert sessions.resolve(conn, result.token) is not None


def test_login_issues_a_new_session_each_time(conn):
    auth.signup(conn, "alice@school.edu", "correct horse battery")
    first = auth.login(conn, "alice@school.edu", "correct horse battery").token
    second = auth.login(conn, "alice@school.edu", "correct horse battery").token

    assert first != second
    assert sessions.resolve(conn, first) is not None
    assert sessions.resolve(conn, second) is not None


def test_every_login_failure_looks_identical(conn):
    """Wrong password, unknown address, passwordless account -- one message."""
    auth.signup(conn, "alice@school.edu", "correct horse battery")
    store.create_user(conn, email="nopassword@school.edu")

    messages = set()
    for email, password in [
        ("alice@school.edu", "wrong password here"),
        ("nobody@school.edu", "correct horse battery"),
        ("nopassword@school.edu", "correct horse battery"),
        ("alice@school.edu", ""),
    ]:
        with pytest.raises(auth.AuthError) as caught:
            auth.login(conn, email, password)
        messages.add(str(caught.value))

    assert messages == {auth.GENERIC_LOGIN_FAILURE}


def test_an_unknown_address_costs_the_same_as_a_wrong_password(conn):
    """The timing half of the same oracle.

    Skipping the hash for unknown addresses returns in microseconds while a real
    rejection takes the ~400ms scrypt costs. That gap is measurable over a
    network and rebuilds exactly what the shared message was hiding. Asserted
    loosely -- the claim is "same order of magnitude", not "identical", since
    wall-clock timing on a shared CI box is noisy.
    """
    auth.signup(conn, "alice@school.edu", "correct horse battery")

    def timed(email):
        start = time.perf_counter()
        with pytest.raises(auth.AuthError):
            auth.login(conn, email, "some wrong password")
        return time.perf_counter() - start

    known = min(timed("alice@school.edu") for _ in range(3))
    unknown = min(timed("nobody@school.edu") for _ in range(3))

    assert unknown > known / 3, (
        f"unknown-address login returned in {unknown*1000:.0f}ms against "
        f"{known*1000:.0f}ms for a known one -- that gap is a membership oracle"
    )


def test_login_upgrades_a_weak_stored_hash(conn):
    """Cost parameters rise over time, and login is the only moment the
    plaintext is in hand to re-hash with."""
    auth.signup(conn, "alice@school.edu", "correct horse battery")

    weak = _hash_with_cost("correct horse battery", n=16384)
    assert passwords.verify("correct horse battery", weak), "the weak hash must be valid"

    account = store.get_user_by_email(conn, "alice@school.edu")
    store.set_password_hash(conn, account["id"], weak)
    assert passwords.needs_rehash(store.get_user(conn, account["id"])["password_hash"])

    auth.login(conn, "alice@school.edu", "correct horse battery")

    upgraded = store.get_user(conn, account["id"])["password_hash"]
    assert passwords.needs_rehash(upgraded) is False
    assert passwords.verify("correct horse battery", upgraded)


def test_logout_ends_the_session(conn):
    result = auth.signup(conn, "alice@school.edu", "correct horse battery")
    assert auth.logout(conn, result.token) is True
    assert sessions.resolve(conn, result.token) is None
    assert auth.logout(conn, result.token) is False


def test_context_for_token(conn):
    result = auth.signup(conn, "alice@school.edu", "correct horse battery")

    context = auth.context_for_token(conn, result.token)
    assert context is not None
    assert context.user_id == result.user.user_id
    assert context.email == "alice@school.edu"

    assert auth.context_for_token(conn, "nonsense") is None
    assert auth.context_for_token(conn, "") is None


def test_context_for_token_never_falls_back_to_a_default_user(conn):
    """An unauthenticated request must fail, not quietly become somebody.

    There is a default local user in the database. If this ever returns it for a
    bad token, every endpoint silently serves that user's data to anonymous
    callers.
    """
    store.get_or_create_default_user(conn)
    for token in ["", "   ", "not-a-token", None]:
        assert auth.context_for_token(conn, token) is None


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("BEARER abc123", "abc123"),
        ("Bearer  abc123 ", "abc123"),
        ("Basic abc123", None),
        ("abc123", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token_parsing(header, expected):
    assert auth.bearer_token(header) == expected
