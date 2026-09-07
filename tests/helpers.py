"""Shared test helpers.

The worker thread is deliberately not started in tests: a background thread
racing assertions is how a suite becomes flaky. Instead these run the same
claim-and-execute step the worker runs, on the calling thread, so a test decides
exactly when a job runs and can assert on the state afterwards.

That works because the queue is in the database rather than in the worker, and
``JobWorker.run_one`` is the whole of what the worker does per job.
"""

from __future__ import annotations

from app.job_runner import JobWorker


def run_pending_jobs(app, limit: int = 10) -> int:
    """Run every queued job synchronously. Returns how many ran.

    ``limit`` stops a job that queues another from looping for ever, which
    would hang the suite rather than fail it.
    """
    settings = app.state.settings
    worker = JobWorker(settings.db_path, settings.data_dir, settings)
    ran = 0
    while ran < limit and worker.run_one(app.state.db):
        ran += 1
    return ran
