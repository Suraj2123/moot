"""The worker loop: what actually runs a queued job.

Kept apart from `jobs.py` on purpose. That module knows how to queue and claim
work without knowing what any of it does; this one knows what the kinds mean and
needs the whole service object to do it. Keeping the split means the queue is
testable without building a StudyLink, which is why its tests run in
milliseconds.

Run it alongside the API:

    python scripts/run_worker.py

One process is enough at this size. More than one is safe -- that is what the
conditional claim is for -- but the connection each holds is not free, and
nothing here is throughput-bound.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Optional

from sqlalchemy import Connection

from . import jobs
from .context import UserContext
from .service import StudyLink

logger = logging.getLogger(__name__)

IDLE_SLEEP = 2.0


def run_job(conn: Connection, job: jobs.Job) -> str:
    """Do the work for one claimed job and return a one-line summary.

    Builds a service object for the job's owner. The user context comes from the
    job row, never from anything ambient -- a worker has no request and no
    session, so there is no other identity it could accidentally pick up.
    """
    app = StudyLink(user=UserContext(user_id=job.user_id, auth_source="job"), conn=conn)

    if job.kind == jobs.KIND_REINDEX:
        stats = app.reindex()
        return (
            f"{stats.notes_chunked} note(s) chunked into {stats.chunks_written} "
            f"chunk(s); embedded {stats.chunk_vectors} chunk(s) and "
            f"{stats.assignment_vectors} assignment(s)"
        )

    if job.kind == jobs.KIND_CANVAS_SYNC:
        result = app.sync_canvas()
        return result.summary

    raise ValueError(f"No handler for job kind {job.kind!r}")


def run_once(conn: Connection) -> Optional[jobs.Job]:
    """Claim and run at most one job. Returns it finished, or None if idle."""
    return jobs.run_one(conn, lambda job: run_job(conn, job))


class Worker:
    """A polling loop with a clean shutdown.

    Polling rather than notifications because it is portable across both
    backends and the latency that matters here is seconds, not milliseconds.
    LISTEN/NOTIFY would work on Postgres and not on SQLite, and having the
    worker behave differently per backend is a worse trade than a 2s poll.
    """

    def __init__(self, conn: Connection, idle_sleep: float = IDLE_SLEEP) -> None:
        self.conn = conn
        self.idle_sleep = idle_sleep
        self.running = False
        self.processed = 0

    def stop(self, *_args) -> None:
        """Ask the loop to finish the current job and then exit."""
        logger.info("worker stopping after the current job")
        self.running = False

    def install_signal_handlers(self) -> None:
        # SIGTERM is what a container runtime sends first. Handling it means a
        # deploy finishes the job in flight instead of stranding it in `running`
        # for reap_stale to fail half an hour later.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)

    def run_forever(self, max_jobs: Optional[int] = None) -> int:
        """Process jobs until stopped. `max_jobs` bounds it, for tests."""
        self.running = True
        while self.running:
            # Housekeeping before claiming: a job stranded by a previous crash
            # should be marked failed rather than sitting in `running` forever.
            jobs.reap_stale(self.conn)

            job = run_once(self.conn)
            if job is None:
                if max_jobs is not None:
                    break
                time.sleep(self.idle_sleep)
                continue

            self.processed += 1
            logger.info("job %s (%s) %s", job.id, job.kind, job.status)
            if max_jobs is not None and self.processed >= max_jobs:
                break

        return self.processed
