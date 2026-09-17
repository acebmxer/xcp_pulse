"""The Diagnostics page: raw per-run detail from the Xen Orchestra API.

Same shape as the Findings page — the *most recent* stored run, not a
history, because a run's artifacts stay downloadable through the job history
after the pool has moved on. Nothing here calls Xen Orchestra; the page
renders what the last job stored.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.activity import log_activity
from app.artifacts import get_artifact, list_for_job
from app.dependencies import (
    login_required,
    operator_required,
    redirect,
    serve_artifact,
    templates,
    wake_worker,
)
from app.job_api_diagnostics import DIAGNOSTICS_ARTIFACT, DIAGNOSTICS_MARKDOWN, report_from_job
from app.job_api_diagnostics import KIND as DIAGNOSTICS_KIND
from app.jobs import enqueue, has_active, latest_job, latest_successful
from app.security import client_ip
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.diagnostics")


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, username: str = Depends(login_required)) -> Response:
    """The latest stored diagnostics run."""
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    job = latest_successful(db, DIAGNOSTICS_KIND)
    payload = report_from_job(db, data_dir, job.id) if job is not None else None
    artifacts = list_for_job(db, job.id) if job is not None else []
    active_job = latest_job(db, DIAGNOSTICS_KIND)

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "username": username,
            "connection": get_connection(db),
            "job": job,
            "report": payload,
            "artifacts": artifacts,
            "json_name": DIAGNOSTICS_ARTIFACT,
            "markdown_name": DIAGNOSTICS_MARKDOWN,
            "running": has_active(db, DIAGNOSTICS_KIND),
            "active_job": active_job,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/diagnostics")
def start_diagnostics(
    request: Request,
    username: str = Depends(operator_required),
    date_preset: str = Form(default=""),
    date_start: str = Form(default=""),
    date_end: str = Form(default=""),
) -> Response:
    """Queue a diagnostics run.

    Refused while one is already going, the same reason a second findings or
    collection run is: the worker runs one job at a time.
    """
    db = request.app.state.db

    if get_connection(db) is None:
        return redirect("/diagnostics?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, DIAGNOSTICS_KIND):
        return redirect("/diagnostics?notice=A+diagnostics+run+is+already+going.")

    job = enqueue(
        db,
        DIAGNOSTICS_KIND,
        {"date_preset": date_preset, "date_start": date_start, "date_end": date_end},
    )
    wake_worker(request)
    log.info("queued %s job %s by %s", DIAGNOSTICS_KIND, job.id, username)
    log_activity(db, username, "diagnostics.start", ip=client_ip(request))
    return redirect("/diagnostics?notice=Reading+diagnostics+from+Xen+Orchestra.")


@router.get("/diagnostics/download/{artifact_id}")
def download_diagnostics(
    artifact_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Serve the stored JSON or Markdown report as a download."""
    db = request.app.state.db
    artifact = get_artifact(db, artifact_id)
    if artifact is not None:
        log.info("%s downloaded %s", username, artifact.name)
        log_activity(db, username, "artifact.download", detail=artifact.name, ip=client_ip(request))
    return serve_artifact(request, artifact_id, on_error="/diagnostics")
