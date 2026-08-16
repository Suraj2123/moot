"""Password hashing.

scrypt from the standard library, not bcrypt or argon2. Those are better
algorithms, but they are C extensions, and this project's whole setup story is
`pip install -r requirements.txt` on a laptop with no compiler. scrypt is
memory-hard, it is in `hashlib`, and it is a genuine KDF rather than a plain
hash -- which is the property that actually matters here.

What is deliberately not done: no pepper, and no rate limit at this layer.
The pepper would live in the same environment as the database credentials, so it
buys little against the attacker who has both. Rate limiting is a property of a
request, not of a hash, so it belongs at the endpoint (commit 8).

Stored format is a single self-describing string:

    scrypt$n=131072,r=8,p=1$<salt-b64>$<hash-b64>

The parameters travel with the hash rather than living in a constant, so raising
them later does not invalidate every existing password. `needs_rehash` reports
which stored hashes are below the current cost, and login upgrades them in place.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# OWASP's floor for scrypt is n=2^17, r=8, p=1, and this matches it. Measured at
# ~420ms and ~128MB per hash on a laptop: slow and memory-hungry enough that
# offline guessing costs real money, fast enough that a login still feels
# instant. n must be a power of two.
#
# The memory figure is the part worth noticing -- it is what makes GPU and ASIC
# attacks expensive, and it is also why concurrent logins are worth bounding
# (commit 8). Sixty simultaneous logins is ~7.7GB.
SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

ALGORITHM = "scrypt"
MIN_PASSWORD_LENGTH = 10

# scrypt needs roughly 128 * n * r bytes. Python caps this per call, and the
# default cap is below what the parameters above need, so it has to be raised
# explicitly or hashing fails with a memory error rather than a wrong answer.
_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2


class PasswordError(ValueError):
    """A password that cannot be accepted, with a reason safe to show a user."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def validate(password: str) -> None:
    """Reject passwords that are not worth hashing.

    Length only. Composition rules (a digit, a symbol, a capital) push people
    toward `Password1!` and measurably do not help, so the one rule here is the
    one that does.
    """
    if not isinstance(password, str) or not password:
        raise PasswordError("Password is required.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password.encode("utf-8")) > 1024:
        # Unbounded input to a memory-hard KDF is a free denial of service.
        raise PasswordError("Password is too long.")


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt. Returns the storable string."""
    validate(password)
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        maxmem=_MAXMEM,
    )
    params = f"n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}"
    return f"{ALGORITHM}${params}${_b64(salt)}${_b64(derived)}"


def _parse(stored: str) -> tuple[dict[str, int], bytes, bytes]:
    algorithm, params, salt, digest = stored.split("$")
    if algorithm != ALGORITHM:
        raise ValueError(f"Unsupported password algorithm {algorithm!r}")
    values = dict(
        (key, int(value))
        for key, value in (part.split("=") for part in params.split(","))
    )
    return values, _unb64(salt), _unb64(digest)


def verify(password: str, stored: str) -> bool:
    """Check a password against a stored hash.

    Returns False rather than raising on a malformed or unknown-algorithm hash:
    a corrupt row should fail the login, not 500 the endpoint and reveal that
    this particular account's record is unusual.
    """
    if not password or not stored:
        return False
    try:
        params, salt, expected = _parse(stored)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=params["n"],
            r=params["r"],
            p=params["p"],
            dklen=len(expected),
            maxmem=128 * params["n"] * params["r"] * 2,
        )
    except (ValueError, KeyError, TypeError):
        return False
    # Constant time: a short-circuiting comparison leaks how much of the digest
    # matched, which is enough to reconstruct it one byte at a time.
    return hmac.compare_digest(derived, expected)


def needs_rehash(stored: str) -> bool:
    """Whether a stored hash was made with weaker parameters than current."""
    try:
        params, _, _ = _parse(stored)
    except (ValueError, KeyError):
        return True
    return (
        params.get("n", 0) < SCRYPT_N
        or params.get("r", 0) < SCRYPT_R
        or params.get("p", 0) < SCRYPT_P
    )
