"""Vault key rotation.

The failure this guards against is dropping the old key too early, which makes
every stored token permanently unreadable. So the script reports what is left,
refuses to claim success while anything failed, and is safe to run twice.
"""

from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rotate_vault_key import key_counts, rotate  # noqa: E402
from studylink import credentials, store, vault  # noqa: E402


def _key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture
def users(conn):
    return [
        store.create_user(conn, email=f"user{i}@school.edu") for i in range(3)
    ]


def test_rotation_re_encrypts_everything(conn, users, monkeypatch):
    old = _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    for i, user_id in enumerate(users):
        credentials.connect_canvas(conn, user_id, "https://canvas.edu", f"token-{i}")

    assert key_counts(conn) == {"v1": 3}

    new = _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new},v1:{old}")

    rotated, failed = rotate(conn)
    assert (rotated, failed) == (3, 0)
    assert key_counts(conn) == {"v2": 3}

    # The tokens still decrypt, to the same values, for the same users.
    for i, user_id in enumerate(users):
        assert credentials.get_token(conn, user_id)[1] == f"token-{i}"


def test_rotated_rows_stay_bound_to_their_user(conn, users, monkeypatch):
    """Re-encryption must not move a secret between accounts."""
    old = _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    credentials.connect_canvas(conn, users[0], "https://canvas.edu", "alice-token")

    new = _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new},v1:{old}")
    rotate(conn)

    assert credentials.get_token(conn, users[0])[1] == "alice-token"
    assert credentials.get_token(conn, users[1]) is None


def test_rotation_is_idempotent(conn, users, monkeypatch):
    old, new = _key(), _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    credentials.connect_canvas(conn, users[0], "https://canvas.edu", "token")

    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new},v1:{old}")
    assert rotate(conn) == (1, 0)
    assert rotate(conn) == (0, 0)


def test_dry_run_changes_nothing(conn, users, monkeypatch):
    old, new = _key(), _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    credentials.connect_canvas(conn, users[0], "https://canvas.edu", "token")

    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new},v1:{old}")
    assert rotate(conn, dry_run=True) == (1, 0)
    assert key_counts(conn) == {"v1": 1}


def test_a_row_whose_key_is_missing_is_reported_not_skipped_silently(
    conn, users, monkeypatch
):
    """Reporting zero failures while leaving rows unreadable is how someone
    drops the old key and loses every token."""
    old, new = _key(), _key()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    credentials.connect_canvas(conn, users[0], "https://canvas.edu", "token")

    # v1 is gone entirely; v3 is loaded but cannot read the old row.
    monkeypatch.setenv(vault.ENV_VAR, f"v3:{new}")

    rotated, failed = rotate(conn)
    assert (rotated, failed) == (0, 1)
    assert key_counts(conn) == {"v1": 1}  # untouched, still needing the old key
