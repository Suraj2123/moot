"""Schema migrations.

A deliberately small runner: numbered `.sql` files in this directory, applied in
filename order, with applied versions recorded in `schema_migrations`. That is
enough for a single-database app and it keeps the schema change visible in the
diff, which is the part that matters when you are about to reshape every table
around a new `user_id`.

When this moves to Postgres (day 2) Alembic replaces this file. The naming
convention here is chosen to survive that: `0001_description.sql` maps onto an
Alembic revision without renaming anything.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


def _applied(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def pending(conn: sqlite3.Connection, directory: Path | None = None) -> list[Path]:
    """Migration files that have not been applied to this database yet."""
    directory = directory or MIGRATIONS_DIR
    applied = _applied(conn)
    return [path for path in sorted(directory.glob("*.sql")) if path.stem not in applied]


def apply_all(conn: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """Apply every pending migration in order. Returns the versions applied.

    Each migration runs in its own transaction: a failure half way through a run
    leaves the earlier migrations applied and recorded, so a re-run picks up
    exactly where it stopped rather than trying to replay work already done.
    """
    applied: list[str] = []
    for path in pending(conn, directory):
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (path.stem, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise RuntimeError(f"migration {path.stem} failed") from None
        applied.append(path.stem)
    return applied
