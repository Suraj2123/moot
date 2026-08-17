#!/usr/bin/env python3
"""Run the background worker.

    python scripts/run_worker.py

Processes queued indexing and Canvas sync jobs until stopped. Handles SIGTERM,
so a deploy finishes the job in flight rather than stranding it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink.config import load_settings  # noqa: E402
from studylink.db import connect  # noqa: E402
from studylink.worker import Worker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one job and exit")
    parser.add_argument("--idle-sleep", type=float, default=2.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    conn = connect(load_settings().sqlalchemy_url)
    worker = Worker(conn, idle_sleep=args.idle_sleep)
    if not args.once:
        worker.install_signal_handlers()
        logging.info("worker started; waiting for jobs")

    processed = worker.run_forever(max_jobs=1 if args.once else None)
    logging.info("processed %d job(s)", processed)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
