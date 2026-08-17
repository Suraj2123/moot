"""The secret vault.

The round-trip test is the least interesting one here. What matters is that a
ciphertext cannot be moved -- between users, or between fields -- and that a
missing key is an error rather than a quiet fallback to plaintext.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from studylink import vault


@pytest.fixture
def key(monkeypatch):
    value = vault.generate_key()
    monkeypatch.setenv(vault.ENV_VAR, value)
    return value


def test_round_trip(key):
    stored = vault.encrypt("canvas-token-123", user_id=1, purpose="canvas")
    assert vault.decrypt(stored, user_id=1, purpose="canvas") == "canvas-token-123"


def test_the_plaintext_is_not_in_the_stored_value(key):
    stored = vault.encrypt("canvas-token-123", user_id=1, purpose="canvas")
    assert "canvas-token-123" not in stored


def test_encrypting_twice_gives_different_ciphertexts(key):
    """A fresh nonce every time. GCM does not merely weaken on nonce reuse --
    it leaks the authentication key."""
    a = vault.encrypt("same value", user_id=1, purpose="canvas")
    b = vault.encrypt("same value", user_id=1, purpose="canvas")
    assert a != b
    assert vault.decrypt(a, 1, "canvas") == vault.decrypt(b, 1, "canvas")


def test_a_ciphertext_cannot_be_moved_to_another_user(key):
    """Without this, moving Alice's encrypted token into Bob's row means Bob's
    sync runs against Alice's Canvas account."""
    stored = vault.encrypt("alice-token", user_id=1, purpose="canvas")

    with pytest.raises(vault.VaultError):
        vault.decrypt(stored, user_id=2, purpose="canvas")


def test_a_ciphertext_cannot_be_moved_to_another_purpose(key):
    stored = vault.encrypt("alice-token", user_id=1, purpose="canvas")

    with pytest.raises(vault.VaultError):
        vault.decrypt(stored, user_id=1, purpose="something-else")


def test_tampering_is_detected(key):
    stored = vault.encrypt("alice-token", user_id=1, purpose="canvas")
    version, nonce, ciphertext = stored.split("$")

    raw = bytearray(base64.b64decode(ciphertext))
    raw[0] ^= 0x01
    tampered = f"{version}${nonce}${base64.b64encode(bytes(raw)).decode()}"

    with pytest.raises(vault.VaultError):
        vault.decrypt(tampered, user_id=1, purpose="canvas")


def test_malformed_values_raise(key):
    for broken in ["", "nonsense", "v1$only-two", "v1$!!!$!!!"]:
        with pytest.raises(vault.VaultError):
            vault.decrypt(broken, user_id=1, purpose="canvas")


def test_a_missing_key_is_an_error_not_a_fallback(monkeypatch):
    """Falling back to plaintext when the key is absent is how secrets end up
    unencrypted in production and nobody notices."""
    monkeypatch.delenv(vault.ENV_VAR, raising=False)

    assert vault.is_configured() is False
    with pytest.raises(vault.VaultNotConfigured):
        vault.encrypt("anything", user_id=1, purpose="canvas")


def test_bad_key_material_is_rejected(monkeypatch):
    for bad in ["no-version", "v1:not-base64!!", "v1:" + base64.b64encode(b"short").decode()]:
        monkeypatch.setenv(vault.ENV_VAR, bad)
        assert vault.is_configured() is False
        with pytest.raises(vault.VaultError):
            vault.encrypt("anything", 1, "canvas")


def test_duplicate_key_versions_are_rejected(monkeypatch):
    one = base64.b64encode(secrets.token_bytes(32)).decode()
    two = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{one},v1:{two}")

    with pytest.raises(vault.VaultError):
        vault.load_keys()


def test_refuses_to_encrypt_nothing(key):
    with pytest.raises(vault.VaultError):
        vault.encrypt("", user_id=1, purpose="canvas")


# ------------------------------------------------------------------- rotation


def test_old_ciphertexts_stay_readable_after_adding_a_new_key(monkeypatch):
    old = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    stored = vault.encrypt("canvas-token", user_id=1, purpose="canvas")

    new = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new},v1:{old}")

    # Readable under the old key...
    assert vault.decrypt(stored, 1, "canvas") == "canvas-token"
    # ...and flagged as due for re-encryption.
    assert vault.needs_rotation(stored) is True

    # New writes use the new key.
    fresh = vault.encrypt("canvas-token", user_id=1, purpose="canvas")
    assert vault.key_version(fresh) == "v2"
    assert vault.needs_rotation(fresh) is False


def test_dropping_a_key_makes_its_ciphertexts_unreadable_loudly(monkeypatch):
    """Retiring a key before re-encrypting must fail with a message that says
    what happened, not silently look like 'Canvas is not connected'."""
    old = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(vault.ENV_VAR, f"v1:{old}")
    stored = vault.encrypt("canvas-token", user_id=1, purpose="canvas")

    new = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv(vault.ENV_VAR, f"v2:{new}")

    with pytest.raises(vault.VaultError, match="which is not loaded"):
        vault.decrypt(stored, 1, "canvas")


def test_generate_key_is_usable_and_random():
    first, second = vault.generate_key(), vault.generate_key()
    assert first != second
    assert vault.load_keys(first)[0].version == "v1"
    assert len(vault.load_keys(first)[0].material) == vault.KEY_BYTES
