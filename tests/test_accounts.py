"""Accounts: addresses, uniqueness, and stored credentials.

The interesting cases are all about case. "Alice@school.edu" and
"alice@school.edu" are the same person everywhere except in a string comparison,
and both engines compare strings case-sensitively by default -- so without a
rule, signing up twice creates two accounts and logging in resolves to whichever
row the database happens to return first.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from studylink import passwords, store


def test_normalise_email_lowercases_and_strips():
    assert store.normalise_email("  Alice@School.EDU ") == "alice@school.edu"
    assert store.normalise_email("") == ""
    assert store.normalise_email(None) == ""


def test_create_user_stores_the_normalised_address(conn):
    user_id = store.create_user(conn, email="  Alice@School.EDU ")
    assert store.get_user(conn, user_id)["email"] == "alice@school.edu"


def test_lookup_is_case_insensitive(conn):
    user_id = store.create_user(conn, email="alice@school.edu")
    for spelling in ["alice@school.edu", "Alice@School.edu", "  ALICE@SCHOOL.EDU  "]:
        found = store.get_user_by_email(conn, spelling)
        assert found is not None, spelling
        assert found["id"] == user_id


def test_the_database_refuses_a_duplicate_address_differing_only_in_case(conn):
    """The index is the guarantee; normalisation on the way in is the convention.

    Both matter. This asserts the guarantee, so that a future code path that
    forgets to normalise fails loudly instead of quietly creating a second
    account that its owner can never reliably log into.
    """
    store.create_user(conn, email="alice@school.edu")
    with pytest.raises(IntegrityError):
        conn.execute(
            store.users.insert().values(
                email="ALICE@SCHOOL.EDU", created_at=store._now()
            )
        )
    conn.rollback()


def test_accounts_without_a_password_are_representable(conn):
    """The seeded local user has no password, and neither will provider logins.

    Null rather than a sentinel: a sentinel is a value, and values eventually
    get compared against and match.
    """
    user_id = store.get_or_create_default_user(conn)
    assert store.get_user(conn, user_id)["password_hash"] is None


def test_password_hash_round_trips_through_the_store(conn):
    stored = passwords.hash_password("correct horse battery")
    user_id = store.create_user(conn, email="alice@school.edu", password_hash=stored)

    loaded = store.get_user_by_email(conn, "Alice@School.edu")
    assert passwords.verify("correct horse battery", loaded["password_hash"]) is True
    assert passwords.verify("wrong", loaded["password_hash"]) is False


def test_set_password_hash_replaces_it(conn):
    user_id = store.create_user(conn, email="alice@school.edu")
    assert store.get_user(conn, user_id)["password_hash"] is None

    store.set_password_hash(conn, user_id, passwords.hash_password("first password!"))
    first = store.get_user(conn, user_id)["password_hash"]
    assert passwords.verify("first password!", first)

    store.set_password_hash(conn, user_id, passwords.hash_password("second password!"))
    second = store.get_user(conn, user_id)["password_hash"]
    assert passwords.verify("second password!", second)
    assert passwords.verify("first password!", second) is False


def test_lookup_of_an_unknown_address_returns_none(conn):
    assert store.get_user_by_email(conn, "nobody@school.edu") is None
    assert store.get_user_by_email(conn, "") is None
