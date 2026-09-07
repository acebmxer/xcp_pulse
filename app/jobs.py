"""Background jobs: the queue, its states, and the record of what each one did.

The queue is a table, not an in-memory structure. That is the whole design:

* a job survives a restart, and one interrupted mid-run is recoverable rather
  than lost with the process;
* the web request that shows progress reads rows, sharing nothing with the
  thread writing them;
* claiming a job is a conditional UPDATE, which SQLite applies atomically, so a
  second consumer — the separate worker process the roadmap anticipates — can
  be added without changing the schema or this module.

This module owns the ``jobs`` table. Nothing else writes to it; a job body
reports what it is doing through the ``JobContext`` handed to it.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A job is in exactly one of these. Terminal states are the ones a job never
# leaves, which is what "is it still going?" means everywhere else.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
ACTIVE_STATES = frozenset({QUEUED, RUNNING})


class JobCancelled(Exception):
    """Raised inside a job body when cancellation has been requested.

    A job cannot be killed from outside — a thread stopped partway through a
    download leaves a half-written file and an open connection. Instead the
    request is recorded, the body notices it at a point where stopping is safe,
    and this unwinds it.
    """


@dataclass(frozen=True)
class Job:
    """One job as the database holds it."""

    id: str
    kind: str
    state: str
    params: dict[str, Any]
    progress: int
    step: str
    error: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    cancel_requested: bool

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_finished(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def duration(self) -> float | None:
        """Seconds the job ran for, or has been running, or None if unstarted."""
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


@dataclass
class JobContext:
    """What a running job body is given to report with.

    A body gets this instead of the connection so that the only writes it can
    make to the ``jobs`` table are progress ones, and so that checking for
    cancellation is a single obvious call rather than a query it has to
    remember to write.
    """

    job_id: str
    conn: sqlite3.Connection
    data_dir: Any
    settings: Any = None
    _last_progress: int = field(default=-1, repr=False)

    def progress(self, percent: int, step: str = "") -> None:
        """Record how far along the job is, and raise if it should stop.

        Every progress call is also a cancellation checkpoint. A body that
        reports progress is therefore cancellable without having to think about
        it, and one that never reports is one that could not be interrupted
        safely anyway.
        """
        self.check_cancelled()
        percent = max(0, min(100, int(percent)))
        # Progress is written far more often than it is read, and an unchanged
        # value is a write for nothing.
        if percent == self._last_progress and not step:
            return
        self._last_progress = percent
        self.conn.execute(
            "UPDATE jobs SET progress = ?, step = ? WHERE id = ?",
            (percent, step, self.job_id),
        )
        self.conn.commit()

    def check_cancelled(self) -> None:
        """Raise JobCancelled if cancellation has been requested."""
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (self.job_id,)
        ).fetchone()
        if row is not None and row["cancel_requested"]:
            raise JobCancelled(self.job_id)


# A job body takes its context and returns nothing; what it produced is stored
# as artifacts against the job id.
JobBody = Callable[[JobContext], None]


def _row_to_job(row: sqlite3.Row) -> Job:
    try:
        params = json.loads(row["params"])
    except (ValueError, TypeError):
        params = {}
    return Job(
        id=row["id"],
        kind=row["kind"],
        state=row["state"],
        params=params if isinstance(params, dict) else {},
        progress=row["progress"],
        step=row["step"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested=bool(row["cancel_requested"]),
    )


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    params: dict[str, Any] | None = None,
) -> Job:
    """Add a job to the queue and return it."""
    job_id = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        """
        INSERT INTO jobs (id, kind, state, params, progress, step, created_at)
        VALUES (?, ?, ?, ?, 0, '', ?)
        """,
        (job_id, kind, QUEUED, json.dumps(params or {}), now),
    )
    conn.commit()
    job = get_job(conn, job_id)
    assert job is not None  # just inserted
    return job


def claim_next(conn: sqlite3.Connection) -> Job | None:
    """Take the oldest queued job and mark it running, or return None.

    The UPDATE carries ``state = 'queued'`` in its WHERE clause, so if another
    consumer claimed the same row first this one changes nothing and moves on.
    That is what makes a second worker — in this process or another — safe to
    add without a lock outside the database.
    """
    while True:
        row = conn.execute(
            "SELECT id FROM jobs WHERE state = ? ORDER BY created_at LIMIT 1",
            (QUEUED,),
        ).fetchone()
        if row is None:
            return None

        cursor = conn.execute(
            "UPDATE jobs SET state = ?, started_at = ? WHERE id = ? AND state = ?",
            (RUNNING, time.time(), row["id"], QUEUED),
        )
        conn.commit()
        if cursor.rowcount == 1:
            return get_job(conn, row["id"])
        # Someone else took it between the SELECT and the UPDATE; look again.


def mark_succeeded(conn: sqlite3.Connection, job_id: str, step: str | None = None) -> None:
    """Record that a job finished its work.

    ``step`` defaults to keeping whatever the body last reported — that final
    message is the job's own summary of what it did ("1 pool(s), 2 host(s)"),
    and the runner, which calls this for every job, has nothing better to say.
    Pass a string to override it.
    """
    if step is None:
        conn.execute(
            """
            UPDATE jobs
               SET state = ?, progress = 100, finished_at = ?, error = NULL
             WHERE id = ?
            """,
            (SUCCEEDED, time.time(), job_id),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
               SET state = ?, progress = 100, step = ?, finished_at = ?, error = NULL
             WHERE id = ?
            """,
            (SUCCEEDED, step, time.time(), job_id),
        )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    """Record that a job stopped on an error, keeping the reason."""
    conn.execute(
        "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
        (FAILED, error, time.time(), job_id),
    )
    conn.commit()


def mark_cancelled(conn: sqlite3.Connection, job_id: str) -> None:
    """Record that a job stopped because cancellation was asked for."""
    conn.execute(
        "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ?",
        (CANCELLED, time.time(), job_id),
    )
    conn.commit()


def request_cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Ask a job to stop. True if it was still active to be asked.

    A queued job is cancelled outright — nothing has started, so there is
    nothing to unwind. A running one is flagged, and stops at its next progress
    checkpoint.
    """
    job = get_job(conn, job_id)
    if job is None or job.is_finished:
        return False

    if job.state == QUEUED:
        conn.execute(
            "UPDATE jobs SET state = ?, cancel_requested = 1, finished_at = ? WHERE id = ?",
            (CANCELLED, time.time(), job_id),
        )
    else:
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    conn.commit()
    return True


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    """One job, or None when there is no such id."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else _row_to_job(row)


def list_jobs(conn: sqlite3.Connection, *, kind: str | None = None, limit: int = 50) -> list[Job]:
    """Recent jobs, newest first, optionally of one kind."""
    if kind is None:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def latest_job(conn: sqlite3.Connection, kind: str) -> Job | None:
    """The most recently created job of one kind, whatever its state."""
    jobs = list_jobs(conn, kind=kind, limit=1)
    return jobs[0] if jobs else None


def latest_successful(conn: sqlite3.Connection, kind: str) -> Job | None:
    """The most recent job of one kind that finished successfully.

    What the dashboard reads: the newest *result*, which is not the newest job
    when the newest one failed or is still running.
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE kind = ? AND state = ? ORDER BY created_at DESC LIMIT 1",
        (kind, SUCCEEDED),
    ).fetchone()
    return None if row is None else _row_to_job(row)


def has_active(conn: sqlite3.Connection, kind: str) -> bool:
    """True when a job of this kind is queued or running.

    Used to refuse a duplicate rather than queue a second identical refresh.
    """
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE kind = ? AND state IN (?, ?) LIMIT 1",
        (kind, QUEUED, RUNNING),
    ).fetchone()
    return row is not None


def reset_orphans(conn: sqlite3.Connection) -> int:
    """Fail jobs left running by a process that stopped. Returns the count.

    A row saying ``running`` after a restart describes a thread that no longer
    exists. Left alone it would show as in-progress for ever and, worse, block
    a new job of the same kind. Called at startup, when nothing can legitimately
    be running yet.
    """
    cursor = conn.execute(
        """
        UPDATE jobs
           SET state = ?, error = ?, finished_at = ?
         WHERE state = ?
        """,
        (
            FAILED,
            "Interrupted — XCP Pulse restarted while this job was running.",
            time.time(),
            RUNNING,
        ),
    )
    conn.commit()
    return cursor.rowcount
