"""Apply migrations before the process that needs them starts.

The tidy way to migrate is a release phase: the platform runs one command
against the new image, once, before any container serves traffic. Fly has it,
Heroku has it, docker-compose fakes it with a one-shot service. Render's
Docker deploys do not offer one below the paid tier, and a platform without
that hook leaves only two options -- migrate by hand after every deploy, or
migrate at boot.

Migrating at boot is usually a bad idea for one specific reason: with several
replicas, every container races to run the same DDL, and Postgres will happily
deadlock two of them against each other. A session-level advisory lock removes
that race. The first container in takes the lock and migrates; the others block
until it finishes, see the schema is already at head, and carry on. So this is
safe to run from the web process and the worker at the same time, which is
exactly what happens on a redeploy.

The lock is Postgres-only. SQLite has one writer by definition and no second
container to race with.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from studylink.config import load_settings  # noqa: E402
from studylink.db import make_engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Arbitrary but fixed: every container has to pick the same number or the lock
# does not serialise anything.
LOCK_KEY = 0x5D0D1_1E5


def main() -> int:
    settings = load_settings()
    url = settings.sqlalchemy_url
    config = Config(str(PROJECT_ROOT / "alembic.ini"))

    if not url.startswith("postgresql"):
        logger.info("migrating %s", url.split("://", 1)[0])
        command.upgrade(config, "head")
        return 0

    engine = make_engine(url)
    try:
        # AUTOCOMMIT because the lock has to outlive the statement that takes
        # it. Inside a transaction the lock is still session-scoped, but the
        # surrounding transaction would sit open for the whole migration and
        # hold its own locks alongside.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            logger.info("waiting for the migration lock")
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
            try:
                logger.info("applying migrations")
                command.upgrade(config, "head")
                logger.info("migrations are at head")
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
