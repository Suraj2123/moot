#!/usr/bin/env python3
"""Delete expired sessions and drop stale rate-limit keys.

Housekeeping, not a security control: `sessions.resolve` already refuses an
expired token, so nothing here changes who can log in. What it changes is that
the table authenticating every request stops growing forever.

Run it from cron, or a scheduled task on whatever hosts this:

    */30 * * * *  cd /srv/studylink && python scripts/purge_sessions.py

`--dry-run` reports what would go without touching anything, which is the only
sensible way to run a delete against production the first time.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from studylink import sessions  # noqa: E402
from studylink.config import load_settings  # noqa: E402
from studylink.db import connect  # noqa: E402
from studylink.schema import sessions as sessions_table  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=0,
        help="only delete sessions that expired this long ago (default: all expired)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(load_settings().sqlalchemy_url)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)

    total = conn.execute(
        select(func.count()).select_from(sessions_table)
    ).scalar_one()
    doomed = conn.execute(
        select(func.count())
        .select_from(sessions_table)
        .where(sessions_table.c.expires_at < cutoff)
    ).scalar_one()

    print(f"sessions:        {total}")
    print(f"expired before:  {cutoff.isoformat()}")
    print(f"to delete:       {doomed}")

    if args.dry_run:
        print("\ndry run, nothing deleted")
        return 0

    deleted = sessions.purge_expired(conn, before=cutoff)
    print(f"deleted:         {deleted}")

    from studylink.ratelimit import limiter

    print(f"rate-limit keys dropped: {limiter.sweep()}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
