"""The job queue.

The claim is the only genuinely hard part, so most of this file is about it: two
workers must never both run the same job, a busy queue must not look empty, and
a worker that dies must not strand work in `running` forever.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from studylink import jobs, store
from studylink.schema import jobs as jobs_table


@pytest.fixture
def alice(conn):
    return store.create_user(conn, email="alice@school.edu")


@pytest.fixture
def bob(conn):
    return store.create_user(conn, email="bob@school.edu")


def test_enqueue_then_read_back(conn, alice):
    job = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    assert job.status == jobs.QUEUED
    assert job.kind == jobs.KIND_REINDEX
    assert jobs.get(conn, job.id, alice).id == job.id


def test_unknown_kinds_are_rejected(conn, alice):
    with pytest.raises(ValueError):
        jobs.enqueue(conn, alice, "delete-everything")


def test_queueing_the_same_work_twice_coalesces(conn, alice):
    """Pressing sync three times should mean one sync."""
    first = jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)
    second = jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)

    assert first.id == second.id
    assert len(jobs.list_for_user(conn, alice)) == 1


def test_coalescing_is_per_user_and_per_kind(conn, alice, bob):
    jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    jobs.enqueue(conn, bob, jobs.KIND_CANVAS_SYNC)

    assert len(jobs.list_for_user(conn, alice)) == 2
    assert len(jobs.list_for_user(conn, bob)) == 1


def test_a_running_job_does_not_block_a_new_one(conn, alice):
    """Work may have arrived since the running job started."""
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    jobs.claim_next(conn)

    second = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    assert second.status == jobs.QUEUED
    assert len(jobs.list_for_user(conn, alice)) == 2


def test_jobs_are_scoped_to_their_owner(conn, alice, bob):
    job = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    assert jobs.get(conn, job.id, bob) is None
    assert jobs.list_for_user(conn, bob) == []


def test_claim_takes_the_oldest_first(conn, alice):
    first = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    second = jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)

    assert jobs.claim_next(conn).id == first.id
    assert jobs.claim_next(conn).id == second.id
    assert jobs.claim_next(conn) is None


def test_a_claimed_job_is_not_claimed_again(conn, alice):
    """The whole point of the conditional UPDATE. Claiming twice means the
    Canvas sync runs twice."""
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    claimed = jobs.claim_next(conn)
    assert claimed is not None
    assert claimed.status == jobs.RUNNING
    assert jobs.claim_next(conn) is None


def _race(conn, monkeypatch, job_id):
    """Make another worker claim `job_id` between our SELECT and our UPDATE.

    Setting the row to running *before* calling claim_next proves nothing: the
    SELECT filters on status, so it would never look at that row and the
    UPDATE's condition would go untested. The race has to happen inside the
    window, which means intercepting the moment the UPDATE is built.
    """
    real_update = jobs.update
    fired = []

    def racing_update(table):
        if not fired:
            fired.append(True)
            conn.execute(
                jobs_table.update()
                .where(jobs_table.c.id == job_id)
                .values(status=jobs.RUNNING, started_at=jobs._now())
            )
        return real_update(table)

    monkeypatch.setattr(jobs, "update", racing_update)
    return fired


def test_a_lost_race_does_not_claim_the_job(conn, alice, monkeypatch):
    """Two workers must never both run one job -- a doubled Canvas sync.

    This is the conditional UPDATE's whole reason for existing, so the race is
    reproduced rather than approximated.
    """
    job = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    fired = _race(conn, monkeypatch, job.id)

    assert jobs.claim_next(conn) is None
    assert fired, "the race never happened; this test proved nothing"


def test_a_busy_queue_does_not_look_empty(conn, alice, monkeypatch):
    """Losing a race must retry, not report an empty queue."""
    contested = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    available = jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)

    fired = _race(conn, monkeypatch, contested.id)

    claimed = jobs.claim_next(conn)
    assert fired, "the race never happened; this test proved nothing"
    assert claimed is not None, "a lost race made a non-empty queue look empty"
    assert claimed.id == available.id


def test_run_one_records_success(conn, alice):
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    finished = jobs.run_one(conn, lambda job: "indexed 3 notes")

    assert finished.status == jobs.SUCCEEDED
    assert finished.detail == "indexed 3 notes"
    assert finished.finished_at is not None


def test_run_one_records_failure_without_dying(conn, alice):
    """A worker that dies on one bad job stops processing every other user's."""
    jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)

    def explode(job):
        raise RuntimeError("Canvas rejected the token")

    finished = jobs.run_one(conn, explode)

    assert finished.status == jobs.FAILED
    assert "Canvas rejected the token" in finished.detail

    # And the worker keeps going.
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    assert jobs.run_one(conn, lambda job: "ok").status == jobs.SUCCEEDED


def test_failure_detail_is_a_message_not_a_traceback(conn, alice):
    """This text is shown to the user, and tracebacks leak paths and sometimes
    credentials out of frame locals."""
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    def explode(job):
        raise RuntimeError("something broke")

    finished = jobs.run_one(conn, explode)
    assert "Traceback" not in finished.detail
    assert "File \"" not in finished.detail


def test_run_one_on_an_empty_queue(conn):
    assert jobs.run_one(conn, lambda job: "never called") is None


def test_detail_is_truncated(conn, alice):
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    finished = jobs.run_one(conn, lambda job: "x" * 10_000)
    assert len(finished.detail) <= jobs.MAX_DETAIL


def test_stale_running_jobs_are_reaped(conn, alice):
    """A killed worker leaves its row running forever otherwise."""
    jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)
    claimed = jobs.claim_next(conn)

    assert jobs.reap_stale(conn) == 0  # not stale yet

    assert jobs.reap_stale(conn, older_than=timedelta(seconds=-1)) == 1
    assert jobs.get(conn, claimed.id, alice).status == jobs.FAILED


def test_reaping_does_not_touch_queued_or_finished_jobs(conn, alice):
    # Run the first job to completion before queueing the second: claim_next
    # takes the oldest, so enqueueing both up front would run the wrong one.
    jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)
    done = jobs.run_one(conn, lambda job: "ok")

    queued = jobs.enqueue(conn, alice, jobs.KIND_REINDEX)

    jobs.reap_stale(conn, older_than=timedelta(seconds=-1))

    assert jobs.get(conn, queued.id, alice).status == jobs.QUEUED
    assert jobs.get(conn, done.id, alice).status == jobs.SUCCEEDED


def test_purge_removes_only_old_finished_jobs(conn, alice):
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    jobs.run_one(conn, lambda job: "ok")
    queued = jobs.enqueue(conn, alice, jobs.KIND_CANVAS_SYNC)

    assert jobs.purge_finished(conn) == 0
    assert jobs.purge_finished(conn, older_than=timedelta(seconds=-1)) == 1
    assert jobs.get(conn, queued.id, alice) is not None


def test_deleting_a_user_takes_their_jobs(conn, alice):
    jobs.enqueue(conn, alice, jobs.KIND_REINDEX)
    conn.execute(store.users.delete().where(store.users.c.id == alice))
    conn.commit()

    assert conn.execute(jobs_table.select()).first() is None
