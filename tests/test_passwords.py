"""Password hashing.

The tests that matter here are the negative ones. A hashing function that
returns True for the right password is easy; the failures that actually cost you
are the ones where it also returns True for something else, or where the stored
form leaks the input.
"""

from __future__ import annotations

import pytest

from studylink import passwords


def test_hash_then_verify_round_trips():
    stored = passwords.hash_password("correct horse battery")
    assert passwords.verify("correct horse battery", stored) is True


def test_wrong_password_fails():
    stored = passwords.hash_password("correct horse battery")
    assert passwords.verify("correct horse batterz", stored) is False
    assert passwords.verify("", stored) is False
    assert passwords.verify("correct horse battery ", stored) is False


def test_the_password_is_not_recoverable_from_the_hash():
    """Obvious, and worth pinning: nothing here should ever store the input."""
    secret = "correct horse battery"
    stored = passwords.hash_password(secret)
    assert secret not in stored
    assert secret.encode("utf-8").hex() not in stored


def test_same_password_hashes_differently_every_time():
    """A fresh salt per hash. Without it, equal hashes reveal equal passwords
    across accounts, and one rainbow table covers the whole user base."""
    a = passwords.hash_password("correct horse battery")
    b = passwords.hash_password("correct horse battery")
    assert a != b
    assert passwords.verify("correct horse battery", a)
    assert passwords.verify("correct horse battery", b)


def test_stored_form_carries_its_parameters():
    """Parameters travel with the hash so raising cost later stays backward
    compatible instead of invalidating every existing password."""
    stored = passwords.hash_password("correct horse battery")
    algorithm, params, salt, digest = stored.split("$")
    assert algorithm == "scrypt"
    assert f"n={passwords.SCRYPT_N}" in params
    assert salt and digest


def test_verify_rejects_a_corrupt_hash_without_raising():
    """A damaged row should fail the login, not 500 the endpoint -- an error
    that only fires for one account is itself a signal about that account."""
    for broken in ["", "not-a-hash", "scrypt$n=1$only-three-parts",
                   "bcrypt$n=16384,r=8,p=1$c2FsdA==$aGFzaA==",
                   "scrypt$n=abc,r=8,p=1$c2FsdA==$aGFzaA=="]:
        assert passwords.verify("anything", broken) is False


def test_short_passwords_are_rejected():
    with pytest.raises(passwords.PasswordError):
        passwords.hash_password("short")
    with pytest.raises(passwords.PasswordError):
        passwords.hash_password("")


def test_absurdly_long_passwords_are_rejected():
    """Unbounded input to a memory-hard KDF is a free denial of service."""
    with pytest.raises(passwords.PasswordError):
        passwords.hash_password("a" * 5000)


def test_needs_rehash_flags_weaker_parameters():
    current = passwords.hash_password("correct horse battery")
    assert passwords.needs_rehash(current) is False

    weaker = current.replace(f"n={passwords.SCRYPT_N}", "n=1024")
    assert passwords.needs_rehash(weaker) is True
    assert passwords.needs_rehash("garbage") is True


def test_unicode_passwords_work():
    stored = passwords.hash_password("正しい馬のバッテリー")
    assert passwords.verify("正しい馬のバッテリー", stored) is True
    assert passwords.verify("正しい馬のバッテリ", stored) is False
