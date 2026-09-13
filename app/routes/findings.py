"""The Findings page: what Xen Orchestra says is wrong, without collecting.

The page shows the *most recent* report rather than a history of them. A
findings run is a snapshot of a pool's current state, so an old one is not a
result worth browsing — it is a description of a pool that has since changed.
The runs themselves are still in the job history, and their artifacts are still
downloadable, because a report attached to a support ticket has to stay
retrievable after the pool has moved on.

Nothing here calls Xen Orchestra. The page renders the stored artifact, so it
loads with XO unreachable and shows the last thing known rather than an error
where the findings were — the same rule the dashboard follows.
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
from app.findings import DEFAULT_WINDOW_DAYS, SEVERITIES, correlate_reports
from app.job_collect import KIND as COLLECT_KIND
from app.job_findings import FINDINGS_ARTIFACT, FINDINGS_MARKDOWN, report_from_job
from app.job_findings import KIND as FINDINGS_KIND
from app.job_log_findings import KIND as LOG_FINDINGS_KIND
from app.job_log_findings import LOG_FINDINGS_ARTIFACT
from app.job_log_findings import report_from_job as log_report_from_job
from app.jobs import enqueue, has_active, latest_job, latest_successful, list_jobs
from app.security import client_ip
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.findings")


@router.get("/findings", response_class=HTMLResponse)
def findings_page(request: Request, username: str = Depends(login_required)) -> Response:
    """The latest stored findings report."""
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    job = latest_successful(db, FINDINGS_KIND)
    report = report_from_job(db, data_dir, job.id) if job is not None else None
    artifacts = list_for_job(db, job.id) if job is not None else []
    active_job = latest_job(db, FINDINGS_KIND)
    log_job = latest_successful(db, LOG_FINDINGS_KIND)
    log_report = log_report_from_job(db, data_dir, log_job.id) if log_job is not None else None
    # The newest *result* is the last successful run; the newest *job* is what
    # says whether the run the operator just started failed. They are different
    # rows once an analysis fails, so a failed run is surfaced from the latter —
    # the same split the dashboard draws for a failed inventory refresh.
    newest_log_job = latest_job(db, LOG_FINDINGS_KIND)
    log_failed = newest_log_job is not None and newest_log_job.state == "failed"
    log_artifacts = [
        artifact
        for collect_job in list_jobs(db, kind=COLLECT_KIND, limit=100)
        for artifact in list_for_job(db, collect_job.id)
        if artifact.name.endswith("-logs.tgz")
    ]

    # Both reports are independent runs, possibly hours apart, describing the
    # same pool from different evidence. Correlating them here, once, means
    # every finding on the page already carries its cross-reference rather
    # than the operator comparing two lists by eye.
    correlate_reports(report, log_report)

    return templates.TemplateResponse(
        request,
        "findings.html",
        {
            "username": username,
            "connection": get_connection(db),
            "job": job,
            "report": report,
            "severities": SEVERITIES,
            "window_days": report.window_days if report else DEFAULT_WINDOW_DAYS,
            "artifacts": artifacts,
            "json_name": FINDINGS_ARTIFACT,
            "markdown_name": FINDINGS_MARKDOWN,
            "running": has_active(db, FINDINGS_KIND),
            "active_job": active_job,
            "log_job": log_job,
            "log_report": log_report,
            "log_artifacts": log_artifacts,
            "log_running": has_active(db, LOG_FINDINGS_KIND),
            "active_log_job": newest_log_job,
            "log_error": newest_log_job.error if log_failed else None,
            "log_findings_artifact": LOG_FINDINGS_ARTIFACT,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/findings/from-logs")
def start_log_findings(
    request: Request,
    artifact_id: str = Form(...),
    username: str = Depends(operator_required),
    date_preset: str = Form(default=""),
    date_start: str = Form(default=""),
    date_end: str = Form(default=""),
) -> Response:
    """Queue findings from one stored collected log bundle."""
    db = request.app.state.db
    artifact = get_artifact(db, artifact_id)
    if artifact is None or not artifact.name.endswith("-logs.tgz"):
        return redirect("/findings?error=That+log+bundle+is+no+longer+stored.")
    if has_active(db, LOG_FINDINGS_KIND):
        return redirect("/findings?notice=A+log+findings+run+is+already+going.")
    job = enqueue(
        db,
        LOG_FINDINGS_KIND,
        {
            "artifact_id": artifact_id,
            "date_preset": date_preset,
            "date_start": date_start,
            "date_end": date_end,
        },
    )
    wake_worker(request)
    log.info("queued %s job %s for %s", LOG_FINDINGS_KIND, job.id, username)
    log_activity(db, username, "findings.from_logs", detail=artifact_id, ip=client_ip(request))
    return redirect("/findings?notice=Reading+findings+from+the+stored+logs.")


@router.post("/findings")
def start_findings(
    request: Request,
    username: str = Depends(operator_required),
    date_preset: str = Form(default=""),
    date_start: str = Form(default=""),
    date_end: str = Form(default=""),
) -> Response:
    """Queue a findings run.

    Refused while one is already going, for the same reason a second collection
    is: the worker runs one job at a time, so a queued duplicate would only sit
    there looking stuck, and two reports of the same pool seconds apart say the
    same thing twice.
    """
    db = request.app.state.db

    if get_connection(db) is None:
        return redirect("/findings?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, FINDINGS_KIND):
        return redirect("/findings?notice=A+findings+run+is+already+going.")

    job = enqueue(
        db,
        FINDINGS_KIND,
        {"date_preset": date_preset, "date_start": date_start, "date_end": date_end},
    )
    wake_worker(request)
    log.info("queued %s job %s by %s", FINDINGS_KIND, job.id, username)
    log_activity(db, username, "findings.start", ip=client_ip(request))
    return redirect("/findings?notice=Reading+findings+from+Xen+Orchestra.")


@router.get("/findings/download/{artifact_id}")
def download_findings(
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
    return serve_artifact(request, artifact_id, on_error="/findings")
