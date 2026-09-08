"""The Collect page: start a collection, download what it produced, clean up.

Downloads are served from here rather than from the jobs page because a bundle
is the point of the application, and burying it in a job's history makes the
operator hunt for the thing they came for.

**The download route streams from disk.** A 433 MB body read into memory to be
returned would undo the care taken everywhere else to never hold one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app import retention
from app.artifacts import get_artifact, human_bytes, list_for_job
from app.dependencies import login_required, redirect, serve_artifact, templates, wake_worker
from app.job_collect import KIND as COLLECT_KIND
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_inventory import inventory_from_job
from app.job_redact import REPORT_ARTIFACT, report_from_job, report_rows
from app.jobs import enqueue, has_active, latest_successful, list_jobs
from app.xo_client import Inventory
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.collect")

# How many collections the page lists. A collection is a large thing an
# operator acts on individually, not a stream of events to scroll.
PAGE_LIMIT = 25


@router.get("/collect", response_class=HTMLResponse)
def collect_page(
    request: Request,
    username: str = Depends(login_required),
    keep_days: int = retention.DEFAULT_KEEP_DAYS,
    keep_count: int = retention.DEFAULT_KEEP_COUNT,
) -> Response:
    """The collect page, with the retention preview for the limits given.

    The limits arrive as query parameters so changing them reloads the preview
    before anything is deleted — the delete button then posts back the same
    numbers the preview was drawn from.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    connection = get_connection(db)
    inventory = _known_inventory(db, data_dir)
    jobs = list_jobs(db, kind=COLLECT_KIND, limit=PAGE_LIMIT)
    plan = retention.plan(db, keep_days=keep_days, keep_count=keep_count)

    return templates.TemplateResponse(
        request,
        "collect.html",
        {
            "username": username,
            "connection": connection,
            "inventory": inventory,
            "hosts": sorted(inventory.hosts, key=lambda host: host.name.lower()),
            "jobs": jobs,
            "artifacts": {job.id: list_for_job(db, job.id) for job in jobs},
            "reports": _reports(db, data_dir, jobs),
            "report_artifact": REPORT_ARTIFACT,
            "plan": plan,
            "human_bytes": human_bytes,
            "collect_kind": COLLECT_KIND,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "any_active": any(job.is_active for job in jobs),
        },
    )


@router.post("/collect")
def start_collection(
    request: Request,
    username: str = Depends(login_required),
    host_id: str = Form(...),
    include_audit: str = Form(default=""),
) -> Response:
    """Queue a collection for one host.

    Refused while another is running: two concurrent 433 MB downloads compete
    for the same disk and the same Xen Orchestra for no gain, and the worker
    runs one job at a time anyway — queueing a second would only leave it
    apparently stuck.

    ``include_audit`` is an unchecked checkbox by default, so an absent field
    means off. It is off because ``xen-bugtool`` already puts ``audit.log`` and
    its rotated copies inside the log bundle, and the separate trail is the
    largest file in a collection.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if get_connection(db) is None:
        return redirect("/collect?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, COLLECT_KIND):
        return redirect("/collect?notice=A+collection+is+already+running.")

    inventory = _known_inventory(db, data_dir)
    host = next((item for item in inventory.hosts if item.id == host_id), None)
    if host is None:
        return redirect(
            "/collect?error=That+host+is+not+in+the+stored+inventory.+"
            "Refresh+the+inventory+and+try+again."
        )

    job = enqueue(
        db,
        COLLECT_KIND,
        {
            "host_id": host.id,
            "host_name": host.name,
            "include_audit": bool(include_audit),
        },
    )
    wake_worker(request)
    log.info("queued %s job %s for host %s by %s", COLLECT_KIND, job.id, host.name, username)
    return redirect(f"/collect?notice=Collecting+from+{host.name}.")


@router.get("/collect/download/{artifact_id}")
def download_artifact(
    artifact_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Serve one stored artifact as a download."""
    artifact = get_artifact(request.app.state.db, artifact_id)
    if artifact is not None:
        log.info("%s downloaded %s (%s)", username, artifact.name, artifact.size_human)
    return serve_artifact(request, artifact_id, on_error="/collect")


@router.post("/collect/{job_id}/delete")
def delete_collection(
    job_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Delete one collection and everything it produced."""
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if retention.delete_collection(db, data_dir, job_id):
        log.info("%s deleted collection %s", username, job_id)
        return redirect("/collect?notice=Collection+deleted.")
    return redirect("/collect?error=There+is+no+such+collection+to+delete.")


@router.post("/collect/cleanup")
def run_cleanup(
    request: Request,
    username: str = Depends(login_required),
    keep_days: int = Form(retention.DEFAULT_KEEP_DAYS),
    keep_count: int = Form(retention.DEFAULT_KEEP_COUNT),
) -> Response:
    """Apply the retention policy, and report what actually went.

    The plan is recomputed inside ``retention.apply`` rather than trusted from
    the page, so a collection finishing between the preview and the button is
    accounted for.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    applied = retention.apply(db, data_dir, keep_days=keep_days, keep_count=keep_count)
    if applied.is_empty:
        return redirect("/collect?notice=Nothing+was+old+enough+to+delete.")

    log.info(
        "%s deleted %d collection(s), freeing %s",
        username,
        len(applied.delete),
        human_bytes(applied.freed_bytes),
    )
    freed = human_bytes(applied.freed_bytes).replace(" ", "+")
    count = len(applied.delete)
    noun = "collection" if count == 1 else "collections"
    return redirect(f"/collect?notice=Deleted+{count}+{noun},+freeing+{freed}.")


def _known_inventory(db, data_dir) -> Inventory:
    """The pools and hosts the last successful refresh stored.

    Read from the stored artifact rather than by calling Xen Orchestra: the
    collect page must not depend on XO being reachable to show what was
    collected, and the host list a collection targets is the one the operator
    already saw on the dashboard.
    """
    job = latest_successful(db, INVENTORY_KIND)
    if job is None:
        return Inventory()
    return inventory_from_job(db, data_dir, job.id) or Inventory()


def _reports(db, data_dir, jobs) -> dict[str, list[dict]]:
    """The per-rule rows for every finished collection on the page.

    The same reader the jobs page uses, because a collection writes the same
    report shape a redaction job does — one format, one renderer.
    """
    rows: dict[str, list[dict]] = {}
    for job in jobs:
        if job.is_active:
            continue
        report = report_from_job(db, data_dir, job.id)
        if report is not None:
            rows[job.id] = report_rows(report)
    return rows
