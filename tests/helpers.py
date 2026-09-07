"""Shared test helpers.

Jobs are run on the calling thread rather than waited for: a background thread
racing assertions is how a suite becomes flaky. These run the same
claim-and-execute step the worker runs, so a test decides exactly when a job
runs and can assert on the state afterwards.

That works because the queue is in the database rather than in the worker, and
``JobWorker.run_one`` is the whole of what the worker does per job.
"""

from __future__ import annotations

from app.job_runner import JobWorker


def run_pending_jobs(app, limit: int = 10) -> int:
    """Run every queued job synchronously. Returns how many ran.

    The app's own worker thread is stopped first. ``create_app`` starts one in
    its lifespan, which ``TestClient`` runs, so without this the two consumers
    race: the background worker can claim the job between the request that
    queued it and this call, leaving nothing to run here and the assertion
    reading a page whose job has not finished. That failed on CI while passing
    locally, which is exactly the shape of bug a synchronous helper exists to
    prevent.

    Stopping it is safe and permanent for the test: every job a test needs is
    run through here, on the calling thread.

    ``limit`` stops a job that queues another from looping for ever, which
    would hang the suite rather than fail it.
    """
    worker_thread = getattr(app.state, "job_worker", None)
    if worker_thread is not None:
        worker_thread.stop()

    settings = app.state.settings
    worker = JobWorker(settings.db_path, settings.data_dir, settings)
    ran = 0
    while ran < limit and worker.run_one(app.state.db):
        ran += 1
    return ran
