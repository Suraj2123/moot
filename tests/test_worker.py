"""The worker loop."""

from __future__ import annotations

from datetime import timedelta

import pytest

from studylink import jobs, store
from studylink.worker import Worker, run_once


@pytest.fixture
def alice(conn):
    return store.get_or_create_default_user(conn)


def test_run_once_on_an_empty_queue(conn):
    assert run_once(conn) is None


def test_the_worker_runs_a_reindex(conn, alice):
    store.create_note(conn, "Note", "Gradient descent steps downhill.", user_id=alice)
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    finished = run_once(conn)
    assert finished.status == jobs.SUCCEEDED
    assert "chunk" in finished.detail


def test_the_worker_takes_its_identity_from_the_job(conn):
    """A worker has no request and no session, so the job row is the only place
    an identity could come from -- and it must not fall back to a default."""
    alice = store.create_user(conn, email="alice@school.edu")
    bob = store.create_user(conn, email="bob@school.edu")

    store.create_note(conn, "Alice", "Gradient descent, ALICE-SECRET.", user_id=alice)
    store.create_note(conn, "Bob", "Gradient descent, BOB-SECRET.", user_id=bob)

    jobs.enqueue(conn, bob, jobs.KIND_REINDEX)
    assert run_once(conn).status == jobs.SUCCEEDED

    from studylink.vectorstore import VectorStore

    vectors = VectorStore(conn)
    assert vectors.count("chunk", "hash-256", bob) > 0
    assert vectors.count("chunk", "hash-256", alice) == 0


def test_the_loop_stops_when_asked(conn, alice):
    for _ in range(2):
        jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
        run_once(conn)

    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    worker = Worker(conn, idle_sleep=0)
    assert worker.run_forever(max_jobs=1) == 1


def test_the_loop_exits_on_an_empty_queue_when_bounded(conn):
    worker = Worker(conn, idle_sleep=0)
    assert worker.run_forever(max_jobs=5) == 0


def test_stop_ends_the_loop(conn, alice):
    worker = Worker(conn, idle_sleep=0)
    worker.stop()
    assert worker.run_forever(max_jobs=10) == 0


def test_the_loop_reaps_stranded_jobs(conn, alice):
    """A job left running by a crashed worker must not sit there forever."""
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    stranded = jobs.claim_next(conn)

    conn.execute(
        jobs.jobs.update()
        .where(jobs.jobs.c.id == stranded.id)
        .values(started_at=jobs._now() - timedelta(hours=2))
    )
    conn.commit()

    Worker(conn, idle_sleep=0).run_forever(max_jobs=1)

    assert jobs.get(conn, stranded.id, alice).status == jobs.FAILED


def test_one_bad_job_does_not_stop_the_worker(conn, alice):
    """Otherwise a single user's broken sync halts everyone else's work."""
    jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)  # no Canvas connected -> fails
    store.create_note(conn, "Note", "Gradient descent.", user_id=alice)
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    worker = Worker(conn, idle_sleep=0)
    assert worker.run_forever(max_jobs=2) == 2

    statuses = [job.status for job in jobs.list_for_user(conn, alice)]
    assert jobs.FAILED in statuses
    assert jobs.SUCCEEDED in statuses
