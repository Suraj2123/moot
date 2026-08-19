"""Per-user Canvas credentials.

The tests worth reading are the ones about a token that must not come back: not
in the status object, not for another user, and not silently as "not connected"
when the key is wrong.
"""

from __future__ import annotations

import pytest

from studylink import credentials, store, vault
from studylink.schema import canvas_credentials


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())


@pytest.fixture
def alice(conn):
    return store.create_user(conn, email="alice@school.edu")


def test_connect_then_read_back(conn, alice, key):
    connection = credentials.connect_canvas(
        conn, alice, "https://canvas.school.edu/", "secret-token"
    )
    assert connection.api_url == "https://canvas.school.edu"
    assert connection.verified_at is None

    url, token = credentials.get_token(conn, alice)
    assert (url, token) == ("https://canvas.school.edu", "secret-token")


def test_the_token_is_encrypted_at_rest(conn, alice, key):
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "secret-token")

    row = conn.execute(canvas_credentials.select()).mappings().first()
    assert "secret-token" not in str(dict(row))
    assert row["key_version"] == "v1"


def test_the_status_object_cannot_hold_a_token(conn, alice, key):
    """A dataclass with no token field cannot leak one through a log line."""
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "secret-token")
    connection = credentials.get_connection(conn, alice)

    assert "secret-token" not in repr(connection)
    assert "secret-token" not in str(connection.as_dict())
    assert not hasattr(connection, "token")


def test_one_users_token_cannot_be_read_as_another(conn, alice, key):
    """The vault binds the ciphertext to the user id, so a bug that passes the
    wrong id fails rather than returning the wrong account's Canvas."""
    bob = store.create_user(conn, email="bob@school.edu")
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "alice-token")

    assert credentials.get_token(conn, bob) is None

    # Even moving the row does not help.
    conn.execute(
        canvas_credentials.update()
        .where(canvas_credentials.c.user_id == alice)
        .values(user_id=bob)
    )
    conn.commit()

    with pytest.raises(credentials.CredentialError):
        credentials.get_token(conn, bob)


def test_reconnecting_replaces_and_unverifies(conn, alice, key):
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "first-token")
    credentials.mark_verified(conn, alice)
    assert credentials.get_connection(conn, alice).verified_at is not None

    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "second-token")

    assert credentials.get_token(conn, alice)[1] == "second-token"
    # The old token working says nothing about the new one.
    assert credentials.get_connection(conn, alice).verified_at is None
    assert conn.execute(canvas_credentials.select()).mappings().all().__len__() == 1


def test_disconnect_forgets_everything(conn, alice, key):
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "secret-token")

    assert credentials.disconnect(conn, alice) is True
    assert credentials.get_connection(conn, alice) is None
    assert credentials.get_token(conn, alice) is None
    assert conn.execute(canvas_credentials.select()).first() is None

    assert credentials.disconnect(conn, alice) is False


def test_deleting_a_user_takes_their_credentials(conn, alice, key):
    credentials.connect_canvas(conn, alice, "https://canvas.school.edu", "secret-token")
    conn.execute(store.users.delete().where(store.users.c.id == alice))
    conn.commit()

    assert conn.execute(canvas_credentials.select()).first() is None


def test_urls_are_validated_and_normalised(conn, alice, key):
    assert credentials.normalise_url("https://canvas.edu/") == "https://canvas.edu"

    for bad in ["", "   ", "canvas.school.edu", "ftp://canvas.edu"]:
        with pytest.raises(credentials.CredentialError):
            credentials.connect_canvas(conn, alice, bad, "token")


def test_an_empty_token_is_rejected(conn, alice, key):
    with pytest.raises(credentials.CredentialError):
        credentials.connect_canvas(conn, alice, "https://canvas.edu", "   ")


def test_connecting_without_an_encryption_key_fails_loudly(conn, alice, monkeypatch):
    """Storing the token in plaintext instead would be strictly worse than
    refusing, and this is the moment that decision gets made."""
    monkeypatch.delenv(vault.ENV_VAR, raising=False)

    with pytest.raises(credentials.CredentialError, match="STUDYLINK_SECRET_KEY"):
        credentials.connect_canvas(conn, alice, "https://canvas.edu", "secret-token")

    assert conn.execute(canvas_credentials.select()).first() is None


def test_connecting_with_a_malformed_key_is_a_stated_error_not_a_crash(
    conn, alice, monkeypatch
):
    """A key that is present but unusable is a server problem, and the user has
    to be told that rather than shown a 500.

    The specific shape here is url-safe base64 with the padding stripped --
    what a plausible key-generation one-liner produces. It looks right, and the
    standard-alphabet decoder rejects it.
    """
    monkeypatch.setenv(vault.ENV_VAR, "v1:tK0qpRXO9nUjWfPmSmU61l_hZhUefI8_LJ2o626kjn0")

    with pytest.raises(credentials.CredentialError, match="server configuration"):
        credentials.connect_canvas(conn, alice, "https://canvas.edu", "secret-token")

    assert conn.execute(canvas_credentials.select()).first() is None


def test_an_undecryptable_token_raises_rather_than_looking_disconnected(
    conn, alice, monkeypatch
):
    """"Not connected" would send the user to reconnect and hide a key problem
    that affects every account on the server."""
    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())
    credentials.connect_canvas(conn, alice, "https://canvas.edu", "secret-token")

    monkeypatch.setenv(vault.ENV_VAR, vault.generate_key())  # different key, same v1

    with pytest.raises(credentials.CredentialError, match="could not be decrypted"):
        credentials.get_token(conn, alice)
