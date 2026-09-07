"""Runs queued jobs. One worker thread, consuming the queue in app/jobs.py.

Why a thread rather than an asyncio task: the Xen Orchestra client is
synchronous httpx, and the job that matters most later is a 100-second download.
Awaiting that on the event loop would freeze every other request; running it on
a thread leaves the web UI responsive without rewriting a client that works.

Why one worker: two concurrent 433 MB downloads would compete for the same disk
and the same XO instance for no gain, and a single worker makes "is this kind of
job already running?" a question with an obvious answer.

**This is one implementation of a consumer, not the design.** The queue lives in
the database and jobs are claimed with an atomic conditional UPDATE, so a
separate worker *process* — which the roadmap anticipates for isolation — can be
added as a second consumer of the same table without changing app/jobs.py, the
schema, or any job body. What would change is only this file: the loop moves out
of the web process, and ``start_worker`` stops being called from the lifespan.
Nothing else here assumes the worker shares a process with the app.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from app.db import connect
from app.jobs import (
    JobCancelled,
    JobContext,
    mark_cancelled,
    mark_failed,
    mark_succeeded,
)
from app.jobs import claim_next as claim_next_job

log = logging.getLogger("xcp_pulse.jobs")

# How long the worker sleeps when the queue is empty. Short enough that a job
# started from the UI begins promptly, long enough to be idle-cheap. The event
# below means an enqueue does not actually wait for it.
_IDLE_POLL_SECONDS = 1.0

# kind -> body. Registered by the modules defining each kind, so this file does
# not import them and cannot become a list everything has to be added to.
_REGISTRY: dict[str, Callable[[JobContext], None]] = {}


def register(kind: str, body: Callable[[JobContext], None]) -> None:
    """Make a job kind runnable. Called at import time by the defining module."""
    _REGISTRY[kind] = body


def registered_kinds() -> list[str]:
    """Every job kind that can be run. Used by tests and by the jobs page."""
    return sorted(_REGISTRY)


class JobWorker:
    """A thread that claims queued jobs and runs them, one at a time.

    It opens its own database connection. SQLite connections are not safe to
    share across threads, and the app's connection belongs to the request path;
    WAL mode — set in ``db.connect`` — is what lets both write without blocking
    each other's reads.
    """

    def __init__(self, db_path: Path, data_dir: Path, settings: object = None) -> None:
        self._db_path = db_path
        self._data_dir = data_dir
        self._settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set on enqueue so a newly queued job starts now rather than after the
        # idle poll interval.
        self._wake = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xcp-pulse-jobs", daemon=True)
        self._thread.start()
        log.info("job worker started (kinds: %s)", ", ".join(registered_kinds()) or "none")

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to finish and wait briefly for it.

        A job in progress is not killed — see JobCancelled in app/jobs.py. The
        worker stops taking new ones and the current one is left to finish; if
        shutdown beats it, ``reset_orphans`` marks it interrupted at next start,
        which is honest about what happened.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("job worker stopped")

    def wake(self) -> None:
        """Tell the worker a job has been queued."""
        self._wake.set()

    def _loop(self) -> None:
        conn = connect(self._db_path)
        try:
            while not self._stop.is_set():
                if not self.run_one(conn):
                    self._wake.wait(_IDLE_POLL_SECONDS)
                    self._wake.clear()
        finally:
            conn.close()

    def run_one(self, conn: sqlite3.Connection) -> bool:
        """Claim and run a single job on the calling thread. True if one ran.

        Public because it is the whole of what the worker does per job, and
        running it directly is how tests execute a job without a background
        thread racing their assertions.
        """
        job = claim_next_job(conn)
        if job is None:
            return False

        body = _REGISTRY.get(job.kind)
        if body is None:
            # A queued kind with no body means a job outlived the code that ran
            # it — an upgrade that dropped a kind, or a typo at the enqueue.
            # Failing it with the reason beats leaving it queued for ever.
            mark_failed(conn, job.id, f"No handler is registered for job kind {job.kind!r}.")
            log.error("job %s has unknown kind %r", job.id, job.kind)
            return True

        context = JobContext(
            job_id=job.id,
            conn=conn,
            data_dir=self._data_dir,
            settings=self._settings,
        )
        log.info("job %s (%s) started", job.id, job.kind)
        try:
            body(context)
        except JobCancelled:
            mark_cancelled(conn, job.id)
            log.info("job %s (%s) cancelled", job.id, job.kind)
        except Exception as exc:
            # A job body failing must never take the worker thread with it: the
            # next job would then never run, with nothing on screen saying why.
            mark_failed(conn, job.id, str(exc) or exc.__class__.__name__)
            log.error("job %s (%s) failed: %s\n%s", job.id, job.kind, exc, traceback.format_exc())
        else:
            mark_succeeded(conn, job.id)
            log.info("job %s (%s) succeeded", job.id, job.kind)
        return True
