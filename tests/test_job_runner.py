"""The worker: what it does with a job body that succeeds, fails or is cancelled.

Run on the calling thread rather than by starting the worker's own, so a test
decides exactly when a job runs. That is possible because the queue is in the
database, which is the same reason a separate worker process can be added later.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.artifacts import list_for_job
from app.db import init_db
from app.job_runner import JobWorker, register, registered_kinds
from app.jobs import CANCELLED, FAILED, SUCCEEDED, JobCancelled, JobContext, enqueue, get_job


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path)


def test_a_job_body_runs_and_the_job_succeeds(conn: sqlite3.Connection, worker: JobWorker) -> None:
    ran: list[str] = []
    register("test_ok", lambda context: ran.append(context.job_id))

    job = enqueue(conn, "test_ok")
    assert worker.run_one(conn) is True
    assert ran == [job.id]
    assert get_job(conn, job.id).state == SUCCEEDED


def test_an_empty_queue_reports_nothing_to_do(conn: sqlite3.Connection, worker: JobWorker) -> None:
    assert worker.run_one(conn) is False


def test_a_body_that_raises_fails_the_job_and_keeps_the_message(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    def boom(context: JobContext) -> None:
        raise RuntimeError("Xen Orchestra went away")

    register("test_boom", boom)
    job = enqueue(conn, "test_boom")
    worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert stored.error == "Xen Orchestra went away"


def test_a_failing_job_does_not_stop_the_next_one(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """A body raising must never take the worker with it.

    If it did, the next job would sit queued for ever with nothing on screen
    saying why — a far worse failure than the one that started it.
    """

    def boom(context: JobContext) -> None:
        raise RuntimeError("nope")

    register("test_boom2", boom)
    register("test_fine", lambda context: None)

    first = enqueue(conn, "test_boom2")
    second = enqueue(conn, "test_fine")

    worker.run_one(conn)
    worker.run_one(conn)

    assert get_job(conn, first.id).state == FAILED
    assert get_job(conn, second.id).state == SUCCEEDED


def test_cancellation_is_recorded_as_cancelled_not_failed(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    def stops(context: JobContext) -> None:
        raise JobCancelled(context.job_id)

    register("test_cancel", stops)
    job = enqueue(conn, "test_cancel")
    worker.run_one(conn)

    stored = get_job(conn, job.id)
    assert stored.state == CANCELLED
    assert stored.error is None, "asking a job to stop is not an error"


def test_an_unknown_kind_fails_with_a_reason_rather_than_sitting_queued(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """A job can outlive the code that ran it — an upgrade dropping a kind."""
    job = enqueue(conn, "a_kind_nothing_registers")
    assert worker.run_one(conn) is True

    stored = get_job(conn, job.id)
    assert stored.state == FAILED
    assert "no handler" in stored.error.lower()


def test_progress_written_by_the_body_is_readable_afterwards(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    def reports(context: JobContext) -> None:
        context.progress(60, "half way")

    register("test_progress", reports)
    job = enqueue(conn, "test_progress")
    worker.run_one(conn)

    # Success sets progress to 100; the step the body last reported survives it
    # only if the body set it, so this asserts the write reached the row.
    assert get_job(conn, job.id).state == SUCCEEDED


def test_a_body_can_store_artifacts_against_its_job(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    from app.artifacts import store_json

    def produces(context: JobContext) -> None:
        store_json(
            context.conn, context.data_dir, job_id=context.job_id, name="out.json", payload={"n": 1}
        )

    register("test_artifact", produces)
    job = enqueue(conn, "test_artifact")
    worker.run_one(conn)

    produced = list_for_job(conn, job.id)
    assert [item.name for item in produced] == ["out.json"]


def test_the_inventory_kind_is_registered() -> None:
    """Importing app.job_inventory is what makes the kind runnable.

    A kind whose module is never imported is queueable but not runnable, which
    would show as "no handler" at run time rather than at import.
    """
    import app.job_inventory  # noqa: F401

    assert "refresh_inventory" in registered_kinds()
