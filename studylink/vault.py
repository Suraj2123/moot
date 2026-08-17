"""Encryption for secrets that have to be readable again.

A Canvas token is not a password. A password is only ever compared, so it gets
hashed and never comes back. A Canvas token has to be sent to Canvas, so it has
to be recoverable -- which means encryption, and encryption means a key that
lives somewhere other than the database.

**This is the one place a third-party dependency is worth it.** Passwords use
stdlib scrypt because the stdlib has a real KDF. The stdlib has no AEAD, and the
alternative to `cryptography` is assembling AES-CTR and HMAC by hand, which is
the single most reliable way to produce something that looks encrypted and is
not. AES-GCM from a maintained library is the boring correct answer.

Three details that are easy to get wrong and are handled here:

**Nonce reuse.** GCM catastrophically fails if a nonce repeats under one key --
not "weakens", but leaks the authentication key. Every encryption draws a fresh
random 96-bit nonce, and nothing reuses one.

**Unbound ciphertext.** Without authenticated additional data, a ciphertext is
just bytes: move Alice's encrypted token into Bob's row and it decrypts happily,
and now Bob's sync runs against Alice's Canvas account. Every ciphertext is
bound to the user id and the purpose it was created for, and decryption with a
different binding fails rather than returning the wrong plaintext.

**Rotation.** Keys are versioned and several can be loaded at once, so a new key
can start being used for writes while old ciphertexts are still readable. See
`scripts/rotate_vault_key.py`.

Key format in the environment:

    STUDYLINK_SECRET_KEY=v1:<32 bytes base64>
    STUDYLINK_SECRET_KEY=v2:<new base64>,v1:<old base64>   # during rotation

The first entry is the write key. The rest are kept for reading only.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Optional

KEY_BYTES = 32
NONCE_BYTES = 12
ENV_VAR = "STUDYLINK_SECRET_KEY"


class VaultError(RuntimeError):
    """Something is wrong with the key material or the ciphertext."""


class VaultNotConfigured(VaultError):
    """No key is set. Raised rather than falling back to storing plaintext."""


@dataclass(frozen=True)
class Key:
    version: str
    material: bytes


def generate_key() -> str:
    """A fresh key in the format the environment variable expects."""
    return "v1:" + base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _parse_keys(raw: str) -> list[Key]:
    keys: list[Key] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        version, _, encoded = entry.partition(":")
        if not version or not encoded:
            raise VaultError(
                f"{ENV_VAR} entries must look like 'v1:<base64>', got {entry[:12]!r}"
            )
        try:
            material = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001 - any decode failure is the same problem
            raise VaultError(f"{ENV_VAR} entry {version} is not valid base64") from exc
        if len(material) != KEY_BYTES:
            raise VaultError(
                f"{ENV_VAR} entry {version} must decode to {KEY_BYTES} bytes, "
                f"got {len(material)}"
            )
        keys.append(Key(version=version, material=material))

    if not keys:
        raise VaultNotConfigured(f"{ENV_VAR} is empty")
    if len({key.version for key in keys}) != len(keys):
        raise VaultError(f"{ENV_VAR} has duplicate key versions")
    return keys


def load_keys(raw: Optional[str] = None) -> list[Key]:
    """Keys from the environment, write key first.

    Read on every call rather than cached at import, so tests and a rotation
    script can change it without reimporting the module.
    """
    value = raw if raw is not None else os.environ.get(ENV_VAR, "")
    if not value.strip():
        raise VaultNotConfigured(
            f"{ENV_VAR} is not set. Generate one with "
            f"`python -c 'from studylink.vault import generate_key; print(generate_key())'`"
        )
    return _parse_keys(value)


def is_configured() -> bool:
    try:
        load_keys()
    except VaultError:
        return False
    return True


def _aad(user_id: int, purpose: str) -> bytes:
    """What a ciphertext is bound to.

    Both parts matter. The user id stops a ciphertext being moved between
    accounts; the purpose stops one encrypted field being pasted into another.
    """
    return f"{int(user_id)}:{purpose}".encode("utf-8")


def encrypt(plaintext: str, user_id: int, purpose: str) -> str:
    """Encrypt with the current write key. Returns a storable string.

    Format: `<version>$<nonce-b64>$<ciphertext-b64>`. The version travels with
    the value so rotation does not need a migration.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not plaintext:
        raise VaultError("Refusing to encrypt an empty value")

    key = load_keys()[0]
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(key.material).encrypt(
        nonce, plaintext.encode("utf-8"), _aad(user_id, purpose)
    )
    return (
        f"{key.version}$"
        f"{base64.b64encode(nonce).decode('ascii')}$"
        f"{base64.b64encode(ciphertext).decode('ascii')}"
    )


def decrypt(stored: str, user_id: int, purpose: str) -> str:
    """Decrypt, verifying the binding. Raises VaultError on any mismatch.

    Raises rather than returning None: a token that will not decrypt is a
    configuration problem or an attack, and both deserve to be loud rather than
    silently becoming "Canvas is not connected".
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        version, nonce_b64, ciphertext_b64 = stored.split("$")
        nonce = base64.b64decode(nonce_b64, validate=True)
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise VaultError("Stored secret is malformed") from exc

    keys = {key.version: key for key in load_keys()}
    key = keys.get(version)
    if key is None:
        raise VaultError(
            f"Secret was encrypted with key {version}, which is not loaded. "
            f"Add it to {ENV_VAR} to read it."
        )

    try:
        plaintext = AESGCM(key.material).decrypt(
            nonce, ciphertext, _aad(user_id, purpose)
        )
    except InvalidTag as exc:
        # Either the ciphertext was altered, or it belongs to a different user or
        # purpose. Both are the same answer to the caller: this is not yours.
        raise VaultError("Secret failed authentication") from exc

    return plaintext.decode("utf-8")


def key_version(stored: str) -> Optional[str]:
    """Which key a stored value used, without decrypting it."""
    version, _, rest = stored.partition("$")
    return version if rest else None


def needs_rotation(stored: str) -> bool:
    """Whether this value is encrypted under something other than the write key."""
    try:
        current = load_keys()[0].version
    except VaultError:
        return False
    return key_version(stored) != current
