#!/usr/bin/env python3
"""Re-encrypt stored secrets under a new vault key.

Rotation is three steps, and the order is what makes it safe:

  1. Generate a new key and put it FIRST, keeping the old one:
         STUDYLINK_SECRET_KEY=v2:<new>,v1:<old>
     New writes now use v2; existing v1 ciphertexts stay readable.

  2. Run this script. It re-encrypts every row still on an old key.

  3. Only once this reports zero remaining, drop the old key:
         STUDYLINK_SECRET_KEY=v2:<new>

Dropping the old key before step 2 finishes makes those rows permanently
unreadable -- the app says so loudly rather than pretending Canvas was never
connected, but the token is gone and every affected user has to reconnect.

    python scripts/rotate_vault_key.py --status
    python scripts/rotate_vault_key.py --dry-run
    python scripts/rotate_vault_key.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from studylink import vault  # noqa: E402
from studylink.config import load_settings  # noqa: E402
from studylink.db import connect, transaction  # noqa: E402
from studylink.schema import canvas_credentials  # noqa: E402


def key_counts(conn) -> dict[str, int]:
    rows = conn.execute(
        select(canvas_credentials.c.key_version, func.count())
        .group_by(canvas_credentials.c.key_version)
    ).all()
    return {version: int(count) for version, count in rows}


def rotate(conn, dry_run: bool = False) -> tuple[int, int]:
    """Re-encrypt every row not on the write key. Returns (rotated, failed)."""
    target = vault.load_keys()[0].version

    rows = conn.execute(
        select(
            canvas_credentials.c.id,
            canvas_credentials.c.user_id,
            canvas_credentials.c.encrypted_token,
        ).where(canvas_credentials.c.key_version != target)
    ).mappings().all()

    rotated = failed = 0
    for row in rows:
        if dry_run:
            rotated += 1
            continue
        try:
            # Decrypt under whichever old key it used, re-encrypt under the new
            # one. The binding to user id and purpose is preserved, so a row
            # cannot be re-encrypted into somebody else's account by accident.
            plaintext = vault.decrypt(
                row["encrypted_token"], row["user_id"], "canvas"
            )
            fresh = vault.encrypt(plaintext, row["user_id"], "canvas")
        except vault.VaultError as exc:
            print(f"  ! row {row['id']} (user {row['user_id']}): {exc}")
            failed += 1
            continue

        with transaction(conn):
            conn.execute(
                canvas_credentials.update()
                .where(canvas_credentials.c.id == row["id"])
                .values(encrypted_token=fresh, key_version=vault.key_version(fresh))
            )
        rotated += 1

    return rotated, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report and exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        keys = vault.load_keys()
    except vault.VaultError as exc:
        print(f"error: {exc}")
        return 1

    conn = connect(load_settings().sqlalchemy_url)
    counts = key_counts(conn)

    print(f"write key:     {keys[0].version}")
    print(f"loaded keys:   {', '.join(key.version for key in keys)}")
    print(f"rows by key:   {counts or '(none)'}")

    stale = sum(count for version, count in counts.items() if version != keys[0].version)
    print(f"needs rotation: {stale}")

    if args.status:
        return 0
    if stale == 0:
        print("\nnothing to do; it is safe to drop the old key")
        return 0

    rotated, failed = rotate(conn, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\ndry run: {rotated} row(s) would be re-encrypted")
        return 0

    print(f"\nre-encrypted: {rotated}")
    if failed:
        print(f"failed:       {failed}")
        print("\nDo NOT drop the old key: some rows could not be re-encrypted.")
        return 1

    print("\nAll rows are on the write key. It is now safe to drop the old key.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
