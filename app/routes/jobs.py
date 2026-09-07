"""The jobs page: what has run, what is running, and starting a refresh.

Progress is read by polling this page rather than pushed over a websocket. A
job's progress lives in the database, so any request can read it; a websocket
would add a connection to keep alive for something that changes a few times a
minute.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.artifacts import list_for_job
from app.dependencies import login_required, redirect, templates
from app.job_inventory import KIND as INVENTORY_KIND
from app.jobs import enqueue, get_job, has_active, list_jobs, request_cancel
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.jobs")

# How many jobs the page shows. Enough to see what happened recently without
# the page becoming a log viewer, which is not what it is for.
PAGE_LIMIT = 25


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, username: str = Depends(login_required)) -> Response:
    db = request.app.state.db
    jobs = list_jobs(db, limit=PAGE_LIMIT)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "username": username,
            "jobs": jobs,
            "artifacts": {job.id: list_for_job(db, job.id) for job in jobs},
            "connection": get_connection(db),
            "inventory_kind": INVENTORY_KIND,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            # Lets the page refresh itself only while there is something to
            # watch, rather than reloading a settled list for ever.
            "any_active": any(job.is_active for job in jobs),
        },
    )


@router.post("/jobs/refresh-inventory")
def start_inventory_refresh(request: Request, username: str = Depends(login_required)) -> Response:
    """Queue an inventory refresh, unless one is already pending."""
    db = request.app.state.db

    if get_connection(db) is None:
        return redirect("/jobs?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, INVENTORY_KIND):
        return redirect("/jobs?notice=An+inventory+refresh+is+already+running.")

    job = enqueue(db, INVENTORY_KIND)
    _wake_worker(request)
    log.info("queued %s job %s for %s", INVENTORY_KIND, job.id, username)
    return redirect("/jobs?notice=Inventory+refresh+queued.")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, username: str = Depends(login_required)) -> Response:
    if request_cancel(request.app.state.db, job_id):
        return redirect("/jobs?notice=Cancellation+requested.")
    return redirect("/jobs?error=That+job+has+already+finished.")


@router.get("/jobs/{job_id}/status")
def job_status(job_id: str, request: Request, username: str = Depends(login_required)) -> Response:
    """One job's state as JSON, for a page watching a job it started."""
    job = get_job(request.app.state.db, job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return JSONResponse(
        {
            "id": job.id,
            "kind": job.kind,
            "state": job.state,
            "progress": job.progress,
            "step": job.step,
            "error": job.error,
            "active": job.is_active,
        }
    )


def _wake_worker(request: Request) -> None:
    """Tell the worker to look now rather than at its next poll.

    Absent when jobs run in a separate process — the queue is the database
    either way — so its absence is not an error.
    """
    worker = getattr(request.app.state, "job_worker", None)
    if worker is not None:
        worker.wake()
