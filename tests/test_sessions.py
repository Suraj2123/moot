"""Session tokens.

Almost every test here is about a token that should *not* work. Issuing a
session and having it resolve is the easy half; the half that matters is that
expired, revoked, unknown, and tampered tokens all fail, and fail the same way.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from studylink import sessions, store


@pytest.fixture
def alice(conn):
    return store.create_user(conn, email="alice@school.edu")


def test_issue_then_resolve(conn, alice):
    token, issued = sessions.create(conn, alice)
    resolved = sessions.resolve(conn, token)

    assert resolved is not None
    assert resolved.user_id == alice
    assert resolved.id == issued.id
    assert resolved.email == "alice@school.edu"


def test_the_token_itself_is_never_stored(conn, alice):
    """A leaked backup must not contain working logins."""
    token, _ = sessions.create(conn, alice)

    rows = conn.execute(sessions.sessions.select()).mappings().all()
    assert len(rows) == 1
    assert token not in str(dict(rows[0]))
    assert rows[0]["token_hash"] == sessions.hash_token(token)


def test_tokens_are_unpredictable(conn, alice):
    issued = {sessions.create(conn, alice)[0] for _ in range(20)}
    assert len(issued) == 20
    assert all(len(t) >= 40 for t in issued)


def test_unknown_tokens_do_not_resolve(conn, alice):
    sessions.create(conn, alice)
    for bogus in ["", "not-a-token", sessions.generate_token(), None]:
        assert sessions.resolve(conn, bogus) is None


def test_a_tampered_token_does_not_resolve(conn, alice):
    token, _ = sessions.create(conn, alice)
    assert sessions.resolve(conn, token + "x") is None
    assert sessions.resolve(conn, token[:-1]) is None
    assert sessions.resolve(conn, token.upper()) is None


def test_expired_sessions_do_not_resolve(conn, alice):
    token, _ = sessions.create(conn, alice, ttl=timedelta(seconds=-1))
    assert sessions.resolve(conn, token) is None


def test_expiry_is_absolute_not_sliding(conn, alice):
    """Using a session must not extend it.

    A session that renews on each use never ends for an attacker who keeps
    using it -- which is exactly the case where you wanted it to end.
    """
    token, issued = sessions.create(conn, alice, ttl=timedelta(minutes=5))
    first = sessions.resolve(conn, token).expires_at
    for _ in range(3):
        sessions.resolve(conn, token)
    assert sessions.resolve(conn, token).expires_at == first


def test_revoked_sessions_do_not_resolve(conn, alice):
    token, _ = sessions.create(conn, alice)
    assert sessions.resolve(conn, token) is not None

    assert sessions.revoke(conn, token) is True
    assert sessions.resolve(conn, token) is None

    # Revoking twice reports that nothing live was ended.
    assert sessions.revoke(conn, token) is False


def test_revocation_keeps_the_row(conn, alice):
    """"Revoked" and "never existed" have to stay distinguishable."""
    token, _ = sessions.create(conn, alice)
    sessions.revoke(conn, token)

    row = conn.execute(sessions.sessions.select()).mappings().first()
    assert row is not None
    assert row["revoked_at"] is not None


def test_revoke_all_ends_every_live_session(conn, alice):
    tokens = [sessions.create(conn, alice)[0] for _ in range(3)]
    assert all(sessions.resolve(conn, t) for t in tokens)

    assert sessions.revoke_all_for_user(conn, alice) == 3
    assert not any(sessions.resolve(conn, t) for t in tokens)

    assert sessions.revoke_all_for_user(conn, alice) == 0


def test_revoke_all_does_not_touch_another_user(conn, alice):
    bob = store.create_user(conn, email="bob@school.edu")
    alice_token, _ = sessions.create(conn, alice)
    bob_token, _ = sessions.create(conn, bob)

    sessions.revoke_all_for_user(conn, alice)

    assert sessions.resolve(conn, alice_token) is None
    assert sessions.resolve(conn, bob_token) is not None


def test_purge_removes_only_expired_rows(conn, alice):
    live, _ = sessions.create(conn, alice, ttl=timedelta(days=1))
    dead, _ = sessions.create(conn, alice, ttl=timedelta(seconds=-1))

    assert sessions.purge_expired(conn) == 1
    assert sessions.resolve(conn, live) is not None
    assert sessions.resolve(conn, dead) is None


@pytest.mark.skipif(
    not __import__("os").environ.get("STUDYLINK_TEST_POSTGRES_URL"),
    reason="set STUDYLINK_TEST_POSTGRES_URL to run the Postgres tests",
)
def test_expiry_works_on_postgres_too():
    """The backends disagree about datetimes, and this code path is auth.

    SQLite hands back naive datetimes, Postgres hands back aware ones, and
    comparing the two raises TypeError. In an expiry check that means a 500 on
    every authenticated request -- loud, but only on the backend the tests were
    not covering. So this covers it.
    """
    import os

    from studylink.db import connect, reset

    pg = connect(os.environ["STUDYLINK_TEST_POSTGRES_URL"])
    try:
        reset(pg)
        user_id = store.create_user(pg, email="pg@school.edu")

        token, _ = sessions.create(pg, user_id)
        resolved = sessions.resolve(pg, token)
        assert resolved is not None
        assert resolved.expires_at.tzinfo is not None

        expired, _ = sessions.create(pg, user_id, ttl=timedelta(seconds=-1))
        assert sessions.resolve(pg, expired) is None

        assert sessions.revoke(pg, token) is True
        assert sessions.resolve(pg, token) is None
        assert sessions.purge_expired(pg) == 1
    finally:
        pg.close()


def test_deleting_a_user_takes_their_sessions(conn, alice):
    """ON DELETE CASCADE, which SQLite only honours with foreign keys enabled."""
    token, _ = sessions.create(conn, alice)
    conn.execute(store.users.delete().where(store.users.c.id == alice))
    conn.commit()

    assert sessions.resolve(conn, token) is None
    assert conn.execute(sessions.sessions.select()).first() is None


def test_purge_honours_a_cutoff(conn, alice):
    """`--older-than-days` keeps a grace period, so a purge run against
    production does not have to be all-or-nothing."""
    from datetime import datetime, timezone

    recently_expired, _ = sessions.create(conn, alice, ttl=timedelta(hours=-1))
    long_expired, _ = sessions.create(conn, alice, ttl=timedelta(days=-10))

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    assert sessions.purge_expired(conn, before=cutoff) == 1

    rows = conn.execute(sessions.sessions.select()).mappings().all()
    assert len(rows) == 1  # the recently expired one survives the grace period


def test_purge_never_removes_a_live_session(conn, alice):
    """The delete is keyed on expires_at, not on revoked_at -- a revoked but
    unexpired session is still a row someone may want to see in their history."""
    live, _ = sessions.create(conn, alice, ttl=timedelta(days=1))
    revoked, _ = sessions.create(conn, alice, ttl=timedelta(days=1))
    sessions.revoke(conn, revoked)

    assert sessions.purge_expired(conn) == 0
    assert sessions.resolve(conn, live) is not None
