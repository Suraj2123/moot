"""Background work: queueing, claiming, and running it.

The database is the queue. At this size that is the right trade -- a broker is a
second thing to run, monitor, and back up, and the whole point of one is
throughput this app does not have.

**The only hard part is claiming a job.** Two workers reading "the oldest queued
row" and both marking it running means the sync runs twice. So the claim is a
single conditional UPDATE:

    UPDATE jobs SET status='running' WHERE id=? AND status='queued'

and the worker owns the job only if that statement reported a row. There is no
window between the read and the write for anyone to slip into, and it works
identically on both backends without needing SELECT FOR UPDATE or advisory
locks. `rowcount` is what makes the decision, which is why the tests check it
rather than the row's contents.

What this deliberately does not have: retries, priorities, scheduling, and
cancellation. Each is real work, and none of it is needed to stop a browser
waiting on a Canvas sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import Connection, and_, delete, select, update

from .db import transaction
from .schema import jobs

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

KIND_REINDEX = "reindex"
KIND_CANVAS_SYNC = "canvas_sync"
KINDS = {KIND_REINDEX, KIND_CANVAS_SYNC}

# How long a running job may go without finishing before it is assumed dead.
# A worker killed mid-job leaves its row in `running` forever otherwise, and
# nothing would ever pick that work up again.
STALE_AFTER = timedelta(minutes=30)

MAX_DETAIL = 2000


@dataclass(frozen=True)
class Job:
    id: int
    user_id: int
    kind: str
    status: str
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    detail: str

    @property
    def finished(self) -> bool:
        return self.status in {SUCCEEDED, FAILED}

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail or "",
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _row_to_job(row) -> Job:
    return Job(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kind=row["kind"],
        status=row["status"],
        created_at=_as_utc(row["created_at"]),
        started_at=_as_utc(row["started_at"]),
        finished_at=_as_utc(row["finished_at"]),
        detail=row["detail"] or "",
    )


def enqueue(conn: Connection, user_id: int, kind: str) -> Job:
    """Queue a job, unless an identical one is already waiting.

    Coalescing rather than queueing duplicates: pressing "sync" three times
    should mean one sync, and three queued reindexes of the same corpus produce
    the same index three times. A job already *running* does not block a new
    one, because work may have arrived since it started.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown job kind {kind!r}. Expected one of {sorted(KINDS)}.")

    existing = conn.execute(
        select(jobs.c.id).where(
            jobs.c.user_id == int(user_id),
            jobs.c.kind == kind,
            jobs.c.status == QUEUED,
        )
    ).first()
    if existing:
        return get(conn, int(existing[0]), user_id)

    with transaction(conn):
        result = conn.execute(
            jobs.insert().values(
                user_id=int(user_id),
                kind=kind,
                status=QUEUED,
                created_at=_now(),
                detail="",
            )
        )
    return get(conn, int(result.inserted_primary_key[0]), user_id)


def get(conn: Connection, job_id: int, user_id: int) -> Optional[Job]:
    """One job, scoped to its owner. Someone else's job is simply not there."""
    row = conn.execute(
        select(jobs).where(
            jobs.c.id == int(job_id), jobs.c.user_id == int(user_id)
        )
    ).mappings().first()
    return _row_to_job(row) if row else None


def list_for_user(conn: Connection, user_id: int, limit: int = 20) -> list[Job]:
    rows = conn.execute(
        select(jobs)
        .where(jobs.c.user_id == int(user_id))
        .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
        .limit(limit)
    ).mappings().all()
    return [_row_to_job(row) for row in rows]


def claim_next(conn: Connection) -> Optional[Job]:
    """Take ownership of the oldest queued job, or return None.

    The claim is a conditional UPDATE whose WHERE clause repeats the status
    check, so two workers racing on the same row cannot both win: the second
    one's UPDATE matches nothing and it moves on.
    """
    while True:
        candidate = conn.execute(
            select(jobs.c.id, jobs.c.user_id)
            .where(jobs.c.status == QUEUED)
            .order_by(jobs.c.created_at, jobs.c.id)
            .limit(1)
        ).first()
        if candidate is None:
            return None

        job_id, user_id = int(candidate[0]), int(candidate[1])
        with transaction(conn):
            result = conn.execute(
                update(jobs)
                .where(and_(jobs.c.id == job_id, jobs.c.status == QUEUED))
                .values(status=RUNNING, started_at=_now())
            )

        if result.rowcount:
            return get(conn, job_id, user_id)
        # Somebody else claimed it between the select and the update. Try again
        # rather than returning None, or a busy queue looks empty.


def finish(conn: Connection, job_id: int, status: str, detail: str = "") -> None:
    if status not in {SUCCEEDED, FAILED}:
        raise ValueError(f"finish() takes {SUCCEEDED} or {FAILED}, got {status!r}")
    with transaction(conn):
        conn.execute(
            update(jobs)
            .where(jobs.c.id == int(job_id))
            .values(status=status, finished_at=_now(), detail=(detail or "")[:MAX_DETAIL])
        )


def reap_stale(conn: Connection, older_than: timedelta = STALE_AFTER) -> int:
    """Fail jobs that have been running too long to still be alive.

    A worker killed mid-job leaves its row in `running` forever, and without
    this nothing ever revisits that work or tells the user it died.
    """
    cutoff = _now() - older_than
    with transaction(conn):
        result = conn.execute(
            update(jobs)
            .where(jobs.c.status == RUNNING, jobs.c.started_at < cutoff)
            .values(
                status=FAILED,
                finished_at=_now(),
                detail="Job did not finish; the worker running it stopped.",
            )
        )
    return int(result.rowcount)


def purge_finished(conn: Connection, older_than: timedelta = timedelta(days=7)) -> int:
    cutoff = _now() - older_than
    with transaction(conn):
        result = conn.execute(
            delete(jobs).where(
                jobs.c.status.in_([SUCCEEDED, FAILED]), jobs.c.finished_at < cutoff
            )
        )
    return int(result.rowcount)


def run_one(conn: Connection, handler: Callable[[Job], str]) -> Optional[Job]:
    """Claim and run a single job. Returns it in its finished state.

    Every exception is caught on purpose. A worker that dies on one bad job
    stops processing every other user's work, so failure is recorded on the row
    and the loop continues. The stored message is `str(exc)` rather than a
    traceback because this text is shown to the user.
    """
    job = claim_next(conn)
    if job is None:
        return None

    try:
        detail = handler(job) or ""
        finish(conn, job.id, SUCCEEDED, detail)
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
        logger.exception("job %s (%s) failed", job.id, job.kind)
        finish(conn, job.id, FAILED, str(exc))

    return get(conn, job.id, job.user_id)
