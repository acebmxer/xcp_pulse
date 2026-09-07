"""The job queue: states, claiming, cancellation and recovery.

These test the store directly rather than through the web UI, because the
properties that matter — a claim being atomic, a failure keeping its reason, a
restart not leaving a job running for ever — are properties of the table, and
the collection job arriving later depends on them being true.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import init_db
from app.jobs import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    JobCancelled,
    JobContext,
    claim_next,
    enqueue,
    get_job,
    has_active,
    latest_job,
    latest_successful,
    list_jobs,
    mark_cancelled,
    mark_failed,
    mark_succeeded,
    request_cancel,
    reset_orphans,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


def test_a_new_job_is_queued(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "refresh_inventory")
    assert job.state == QUEUED
    assert job.progress == 0
    assert job.is_active
    assert not job.is_finished


def test_params_survive_the_round_trip(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "collect", {"host_id": "host-1", "rotated": True})
    assert get_job(conn, job.id).params == {"host_id": "host-1", "rotated": True}


def test_claiming_takes_the_oldest_job_first(conn: sqlite3.Connection) -> None:
    first = enqueue(conn, "a")
    second = enqueue(conn, "b")

    assert claim_next(conn).id == first.id
    assert claim_next(conn).id == second.id
    assert claim_next(conn) is None


def test_a_claimed_job_is_running_and_cannot_be_claimed_again(
    conn: sqlite3.Connection,
) -> None:
    """The property a second worker process depends on.

    Claiming is a conditional UPDATE on state, so once a job is running no
    other consumer can take it — which is what makes moving the worker out of
    this process later a deployment change rather than a redesign.
    """
    enqueue(conn, "refresh_inventory")
    claimed = claim_next(conn)

    assert claimed.state == RUNNING
    assert claimed.started_at is not None
    assert claim_next(conn) is None, "a running job must not be claimable again"


def test_a_failure_keeps_its_reason(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_failed(conn, job.id, "cannot reach https://xo.example.com")

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert stored.error == "cannot reach https://xo.example.com"
    assert stored.is_finished


def test_success_sets_progress_to_complete(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_succeeded(conn, job.id, "1 pool(s), 2 host(s)")

    stored = get_job(conn, job.id)
    assert stored.state == SUCCEEDED
    assert stored.progress == 100
    assert stored.error is None


def test_success_keeps_what_the_body_last_reported(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The body's final message is the job's own summary of what it did.

    The runner marks every job succeeded and has nothing better to say, so
    overwriting the step there would throw away the only line describing the
    result — leaving a succeeded job on the page with nothing under it.
    """
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    JobContext(job_id=job.id, conn=conn, data_dir=tmp_path).progress(100, "1 pool(s), 2 host(s)")

    mark_succeeded(conn, job.id)
    assert get_job(conn, job.id).step == "1 pool(s), 2 host(s)"


def test_success_can_override_the_step(conn: sqlite3.Connection, tmp_path: Path) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    JobContext(job_id=job.id, conn=conn, data_dir=tmp_path).progress(90, "part way")

    mark_succeeded(conn, job.id, "done")
    assert get_job(conn, job.id).step == "done"


def test_cancelling_a_queued_job_ends_it_outright(conn: sqlite3.Connection) -> None:
    """Nothing has started, so there is nothing to unwind."""
    job = enqueue(conn, "refresh_inventory")

    assert request_cancel(conn, job.id) is True
    assert get_job(conn, job.id).state == CANCELLED
    assert claim_next(conn) is None, "a cancelled job must not then be run"


def test_cancelling_a_running_job_flags_it_for_its_next_checkpoint(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A running job is asked to stop, not killed.

    Killing a thread mid-download leaves a half-written file and an open
    connection, so the request is recorded and the body notices it where
    stopping is safe.
    """
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)

    assert request_cancel(conn, job.id) is True
    assert get_job(conn, job.id).state == RUNNING, "it stops at a checkpoint, not immediately"

    context = JobContext(job_id=job.id, conn=conn, data_dir=tmp_path)
    with pytest.raises(JobCancelled):
        context.progress(50, "part way")


def test_cancelling_a_finished_job_reports_that_it_was_too_late(
    conn: sqlite3.Connection,
) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_succeeded(conn, job.id)

    assert request_cancel(conn, job.id) is False


def test_progress_is_recorded_and_clamped(conn: sqlite3.Connection, tmp_path: Path) -> None:
    job = enqueue(conn, "refresh_inventory")
    context = JobContext(job_id=job.id, conn=conn, data_dir=tmp_path)

    context.progress(40, "Reading pools and hosts")
    stored = get_job(conn, job.id)
    assert stored.progress == 40
    assert stored.step == "Reading pools and hosts"

    context.progress(300, "over")
    assert get_job(conn, job.id).progress == 100


def test_a_restart_does_not_leave_a_job_running_for_ever(conn: sqlite3.Connection) -> None:
    """A row saying "running" after a restart describes a thread that is gone.

    Left alone it would show as in progress indefinitely and, worse, block a
    new job of the same kind from being started.
    """
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)

    assert reset_orphans(conn) == 1
    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "restarted" in stored.error
    assert not has_active(conn, "refresh_inventory")


def test_reset_orphans_leaves_finished_jobs_alone(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_succeeded(conn, job.id)

    assert reset_orphans(conn) == 0
    assert get_job(conn, job.id).state == SUCCEEDED


def test_has_active_is_what_refuses_a_duplicate(conn: sqlite3.Connection) -> None:
    assert has_active(conn, "refresh_inventory") is False
    job = enqueue(conn, "refresh_inventory")
    assert has_active(conn, "refresh_inventory") is True

    claim_next(conn)
    assert has_active(conn, "refresh_inventory") is True, "running still counts"

    mark_succeeded(conn, job.id)
    assert has_active(conn, "refresh_inventory") is False


def test_latest_successful_skips_a_newer_failure(conn: sqlite3.Connection) -> None:
    """What the dashboard reads: the newest result, not the newest attempt."""
    good = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_succeeded(conn, good.id)

    bad = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_failed(conn, bad.id, "XO went away")

    assert latest_job(conn, "refresh_inventory").id == bad.id
    assert latest_successful(conn, "refresh_inventory").id == good.id


def test_list_jobs_is_newest_first_and_filters_by_kind(conn: sqlite3.Connection) -> None:
    enqueue(conn, "refresh_inventory")
    collect = enqueue(conn, "collect")

    assert list_jobs(conn)[0].id == collect.id
    assert [job.kind for job in list_jobs(conn, kind="collect")] == ["collect"]


def test_cancelled_jobs_are_finished_not_failed(conn: sqlite3.Connection) -> None:
    job = enqueue(conn, "refresh_inventory")
    claim_next(conn)
    mark_cancelled(conn, job.id)

    stored = get_job(conn, job.id)
    assert stored.state == CANCELLED
    assert stored.is_finished
    assert stored.error is None, "cancelling is not an error and must not read as one"
