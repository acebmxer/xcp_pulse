"""The jobs page: what has run, what is running, and starting a refresh.

Progress is read by polling this page rather than pushed over a websocket. A
job's progress lives in the database, so any request can read it; a websocket
would add a connection to keep alive for something that changes a few times a
minute.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import retention
from app.activity import log_activity
from app.artifacts import get_artifact, list_for_job
from app.dependencies import (
    age,
    login_required,
    operator_required,
    redirect,
    serve_artifact,
    templates,
    wake_worker,
)
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_redact import KIND as REDACT_KIND
from app.job_redact import (
    REDACTED_MARKER,
    REPORT_ARTIFACT,
    existing_redaction,
    report_from_job,
    report_rows,
)
from app.jobs import enqueue, get_job, has_active, list_jobs, request_cancel
from app.redact import RULES, enabled_rules
from app.security import client_ip
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.jobs")

# How many jobs the page shows. Enough to see what happened recently without
# the page becoming a log viewer, which is not what it is for.
PAGE_LIMIT = 25


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, username: str = Depends(login_required)) -> Response:
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir
    jobs = list_jobs(db, limit=PAGE_LIMIT)
    artifacts = {job.id: list_for_job(db, job.id) for job in jobs}
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "username": username,
            "jobs": jobs,
            "artifacts": artifacts,
            # Only redaction jobs have a report, and only a finished one has a
            # stored file to read, so the page asks for no others.
            "reports": _reports(db, data_dir, jobs),
            "redactable": _redactable(artifacts),
            "connection": get_connection(db),
            "inventory_kind": INVENTORY_KIND,
            "redact_kind": REDACT_KIND,
            "report_artifact": REPORT_ARTIFACT,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            # Lets the page refresh itself only while there is something to
            # watch, rather than reloading a settled list for ever.
            "any_active": any(job.is_active for job in jobs),
        },
    )


@router.post("/jobs/refresh-inventory")
def start_inventory_refresh(
    request: Request, username: str = Depends(operator_required)
) -> Response:
    """Queue an inventory refresh, unless one is already pending."""
    db = request.app.state.db

    if get_connection(db) is None:
        return redirect("/jobs?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, INVENTORY_KIND):
        return redirect("/jobs?notice=An+inventory+refresh+is+already+running.")

    job = enqueue(db, INVENTORY_KIND)
    wake_worker(request)
    log.info("queued %s job %s for %s", INVENTORY_KIND, job.id, username)
    log_activity(db, username, "inventory.refresh", ip=client_ip(request))
    return redirect("/jobs?notice=Inventory+refresh+queued.")


@router.post("/jobs/redact")
def start_redaction(
    request: Request,
    username: str = Depends(operator_required),
    artifact_id: str = Form(...),
) -> Response:
    """Queue a redaction of one stored artifact.

    The artifact is checked here rather than only in the job body so a bad id
    is a message on the page instead of a failed job the operator has to go
    and read.
    """
    db = request.app.state.db

    if get_artifact(db, artifact_id) is None:
        return redirect("/jobs?error=That+file+is+no+longer+stored.")

    if has_active(db, REDACT_KIND):
        return redirect("/jobs?notice=A+redaction+is+already+running.")

    # Same file, same rules, same answer. Refused rather than warned: the
    # duplicate costs a second copy of the source's whole size on the data
    # volume and tells the operator nothing the first run did not.
    data_dir = request.app.state.settings.data_dir
    enabled = enabled_rules(db)
    earlier = existing_redaction(db, data_dir, artifact_id, enabled)
    if earlier is not None:
        return redirect(f"/jobs?error={quote_plus(_duplicate_message(earlier, enabled))}")

    job = enqueue(db, REDACT_KIND, {"artifact_id": artifact_id})
    wake_worker(request)
    log.info("queued %s job %s for %s", REDACT_KIND, job.id, username)
    log_activity(db, username, "redact.start", detail=artifact_id, ip=client_ip(request))
    return redirect("/jobs?notice=Redaction+queued.")


@router.get("/jobs/download/{artifact_id}")
def download_job_artifact(
    artifact_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Serve one stored artifact as a download."""
    db = request.app.state.db
    artifact = get_artifact(db, artifact_id)
    if artifact is not None:
        log.info("%s downloaded %s (%s)", username, artifact.name, artifact.size_human)
        log_activity(db, username, "artifact.download", detail=artifact.name, ip=client_ip(request))
    return serve_artifact(request, artifact_id, on_error="/jobs")


@router.post("/jobs/{job_id}/delete")
def delete_redaction(
    job_id: str,
    request: Request,
    username: str = Depends(operator_required),
) -> Response:
    """Delete one redaction and the files it produced.

    Restricted to redaction jobs: a collection is deleted from the collect
    page, which shows its size and what the copy is for, and deleting one from
    here would hide a 2.3 GiB removal behind a button in a job list.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if retention.delete_job(db, data_dir, job_id, kind=REDACT_KIND):
        log.info("%s deleted redaction %s", username, job_id)
        log_activity(db, username, "redact.delete", detail=job_id, ip=client_ip(request))
        return redirect("/jobs?notice=Redaction+deleted.")
    return redirect("/jobs?error=There+is+no+such+redaction+to+delete.")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str, request: Request, username: str = Depends(operator_required)
) -> Response:
    db = request.app.state.db
    if request_cancel(db, job_id):
        log_activity(db, username, "job.cancel", detail=job_id, ip=client_ip(request))
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


def _duplicate_message(earlier, enabled) -> str:
    """Why a redaction was refused, naming the run that already answers it.

    Says when it ran and which rules were off, because those are what decide
    whether the operator wants a different result or already has the one they
    need. A refusal naming nothing makes them scroll the history to find out
    whether the earlier copy is even still stored.
    """
    off = [rule.title for rule in RULES if rule.name not in enabled]
    if not off:
        rules = "every rule switched on"
    elif len(off) == 1:
        rules = f"{off[0]} switched off"
    else:
        rules = f"{len(off)} rules switched off ({', '.join(off)})"

    return (
        f"That file was already redacted {age(earlier.finished_at)}, with {rules} — "
        f"the same rules as now, so a second run would produce an identical copy. "
        f"Its files are in the history below. Change a rule in Redaction, or delete "
        f"that redaction, to run it again."
    )


def _reports(db, data_dir, jobs) -> dict[str, list[dict]]:
    """The per-rule rows for every finished redaction job on the page.

    Read here rather than in the template because it opens a file per job, and
    a template that reads the disk is a template nobody can reason about.
    """
    rows: dict[str, list[dict]] = {}
    for job in jobs:
        if job.kind != REDACT_KIND or job.is_active:
            continue
        report = report_from_job(db, data_dir, job.id)
        if report is not None:
            rows[job.id] = report_rows(report)
    return rows


def _redactable(artifacts: dict) -> list:
    """Stored artifacts that can be offered for redaction, newest first.

    A redaction's own output is excluded: redacting a redacted copy again masks
    nothing and only adds a file, and the report is JSON this application
    wrote, not log text from a host.
    """
    candidates = [item for produced in artifacts.values() for item in produced]
    return sorted(
        (
            item
            for item in candidates
            if item.name != REPORT_ARTIFACT and f".{REDACTED_MARKER}." not in item.name
        ),
        key=lambda item: item.created_at,
        reverse=True,
    )
