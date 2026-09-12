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
from app.job_extract import KIND as EXTRACT_KIND
from app.job_extract import REPORT_ARTIFACT as EXTRACT_REPORT_ARTIFACT
from app.job_extract import report_from_job as extract_report_from_job
from app.job_inventory import known_inventory
from app.job_redact import REPORT_ARTIFACT, existing_redaction, report_from_job, report_rows
from app.jobs import enqueue, get_job, has_active, list_jobs
from app.log_categories import CATEGORIES
from app.redact import enabled_rules
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.collect")

# A checked-box list arrives as zero or more repeated form fields. Shared as a
# module-level default, the same way redaction.py's rule checkboxes are,
# rather than a mutable default argument.
_CATEGORIES_FIELD = Form(default_factory=list)

# How many collections the page lists. A collection is a large thing an
# operator acts on individually, not a stream of events to scroll.
PAGE_LIMIT = 25

# How many extractions the page lists per collection they were drawn from.
# An operator picking apart one bundle several times over a support ticket is
# the case that matters; nothing here is meant to become a second job history.
EXTRACTIONS_PER_COLLECTION = 10


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
    inventory = known_inventory(db, data_dir)
    jobs = list_jobs(db, kind=COLLECT_KIND, limit=PAGE_LIMIT)
    plan = retention.plan(db, keep_days=keep_days, keep_count=keep_count)
    extractions = list_jobs(db, kind=EXTRACT_KIND, limit=PAGE_LIMIT)
    # Keyed by both collection and extraction job ids: the template reads an
    # extraction's own produced files the same way it reads a collection's,
    # and an extraction id absent from this dict was rendering with no
    # download link at all — reported from a real run.
    artifacts = {job.id: list_for_job(db, job.id) for job in [*jobs, *extractions]}

    # A collection redacted by a separate, chained `redact_artifact` job — the
    # "Redact now" button, or a fresh raw-only collection — has its redacted
    # copy and report stored under *that job's* id, not the collection's.
    # Found the same rules-aware way the Jobs page finds a duplicate to
    # refuse re-redacting one, so the card shows what actually happened
    # instead of reading "not redacted" forever after it plainly was.
    redaction_jobs = _chained_redactions(db, data_dir, jobs)
    for job_id, redact_job_id in redaction_jobs.items():
        artifacts[job_id] = [*artifacts[job_id], *list_for_job(db, redact_job_id)]

    return templates.TemplateResponse(
        request,
        "collect.html",
        {
            "username": username,
            "connection": connection,
            "inventory": inventory,
            "hosts": sorted(inventory.hosts, key=lambda host: host.name.lower()),
            "jobs": jobs,
            "artifacts": artifacts,
            "raw_bundles": _raw_bundles(artifacts),
            "reports": _reports(db, data_dir, jobs, chained=redaction_jobs),
            "report_artifact": REPORT_ARTIFACT,
            "extract_report_artifact": EXTRACT_REPORT_ARTIFACT,
            "plan": plan,
            "human_bytes": human_bytes,
            "collect_kind": COLLECT_KIND,
            "categories": CATEGORIES,
            "category_titles": {category.key: category.title for category in CATEGORIES},
            "extractions": _extractions_by_source(db, extractions),
            "extractions_per_collection": EXTRACTIONS_PER_COLLECTION,
            "extraction_reports": _reports(
                db, data_dir, extractions, reader=extract_report_from_job
            ),
            "extract_kind": EXTRACT_KIND,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "any_active": any(job.is_active for job in jobs)
            or any(job.is_active for job in extractions),
        },
    )


@router.post("/collect")
def start_collection(
    request: Request,
    username: str = Depends(login_required),
    host_id: str = Form(...),
    include_audit: str = Form(default=""),
    redact: str = Form(default=""),
    categories: list[str] = _CATEGORIES_FIELD,
    include_rotated: str = Form(default=""),
) -> Response:
    """Queue a collection for one host, and optionally an extraction after it.

    Refused while another collection or extraction is running: two concurrent
    433 MB downloads compete for the same disk and the same Xen Orchestra for
    no gain, and the worker runs one job at a time anyway — queueing a second
    would only leave it apparently stuck.

    ``include_audit`` is an unchecked checkbox by default, so an absent field
    means off. It is off because ``xen-bugtool`` already puts ``audit.log`` and
    its rotated copies inside the log bundle, and the separate trail is the
    largest file in a collection.

    ``redact`` follows the same unticked-checkbox-sends-nothing convention as
    ``include_audit``: a submitted form only carries the field when it was
    ticked, so an absent field here means off. The page renders this one
    pre-ticked, so in practice it comes back off only when an operator
    deliberately unticks it before submitting.

    ``categories``, when any are ticked, queues an extraction job right behind
    the collection, addressed by ``source_job_id`` rather than an artifact id —
    the collection has not produced its bundle yet at this point in the
    request. See ``job_extract.run`` for how that is resolved once the
    collection has actually finished.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if get_connection(db) is None:
        return redirect("/collect?error=Configure+a+Xen+Orchestra+connection+first.")

    if has_active(db, COLLECT_KIND) or has_active(db, EXTRACT_KIND):
        return redirect("/collect?notice=A+collection+is+already+running.")

    inventory = known_inventory(db, data_dir)
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
            "redact": bool(redact),
        },
    )
    wake_worker(request)
    log.info("queued %s job %s for host %s by %s", COLLECT_KIND, job.id, host.name, username)

    keys = _valid_category_keys(categories)
    if keys:
        extract_job = enqueue(
            db,
            EXTRACT_KIND,
            {
                "source_job_id": job.id,
                "categories": keys,
                "include_rotated": bool(include_rotated),
            },
        )
        log.info(
            "queued %s job %s after collection %s for %s",
            EXTRACT_KIND,
            extract_job.id,
            job.id,
            username,
        )

    return redirect(f"/collect?notice=Collecting+from+{host.name}.")


@router.post("/collect/{job_id}/extract")
def start_extraction(
    job_id: str,
    request: Request,
    username: str = Depends(login_required),
    categories: list[str] = _CATEGORIES_FIELD,
    include_rotated: str = Form(default=""),
) -> Response:
    """Queue an extraction from one already-stored collection's raw bundle.

    ``job_id`` names the collection, not an artifact directly, because that is
    what the Collect page's card is keyed by — the raw bundle inside it is
    found the same way ``job_extract._resolve_source`` finds it for the
    queued-alongside-a-collection path, but here the collection has already
    finished, so it is looked up eagerly rather than deferred to the job body:
    a bad or deleted collection is a message on this page, not a failed job the
    operator has to go and read.
    """
    db = request.app.state.db

    keys = _valid_category_keys(categories)
    if not keys:
        return redirect("/collect?error=Pick+at+least+one+log+category+to+extract.")

    job = get_job(db, job_id)
    if job is None or job.kind != COLLECT_KIND:
        return redirect("/collect?error=That+collection+is+no+longer+stored.")

    bundle = next(
        (item for item in list_for_job(db, job_id) if item.name.endswith("-logs.tgz")),
        None,
    )
    if bundle is None:
        return redirect("/collect?error=That+collection+has+no+log+bundle+to+extract+from.")

    if has_active(db, COLLECT_KIND) or has_active(db, EXTRACT_KIND):
        return redirect("/collect?notice=A+collection+is+already+running.")

    extract_job = enqueue(
        db,
        EXTRACT_KIND,
        {
            "artifact_id": bundle.id,
            "categories": keys,
            "include_rotated": bool(include_rotated),
        },
    )
    wake_worker(request)
    log.info(
        "queued %s job %s for %s from collection %s",
        EXTRACT_KIND,
        extract_job.id,
        username,
        job_id,
    )
    return redirect("/collect?notice=Extracting+the+selected+categories.")


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


@router.post("/collect/extractions/{job_id}/delete")
def delete_extraction(
    job_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Delete one extraction and the file it produced.

    A separate route from ``delete_collection`` — restricted to
    ``EXTRACT_KIND`` — because the two are different rows in the jobs table
    with different files behind them, and an extraction's id must never be
    able to delete the collection it was drawn from.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if retention.delete_job(db, data_dir, job_id, kind=EXTRACT_KIND):
        log.info("%s deleted extraction %s", username, job_id)
        return redirect("/collect?notice=Extraction+deleted.")
    return redirect("/collect?error=There+is+no+such+extraction+to+delete.")


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


def _reports(db, data_dir, jobs, *, reader=report_from_job, chained=None) -> dict[str, list[dict]]:
    """The per-rule rows for every finished job on the page.

    The same reader the jobs page uses by default, because a collection writes
    the same report shape a redaction job does — one format, one renderer.
    ``reader`` is swapped for extractions, whose report carries the same rule
    rows plus which categories were pulled, so ``report_rows`` still applies.

    ``chained``, when given, maps a collection's id to the separate
    ``redact_artifact`` job that actually redacted it — see
    ``_chained_redactions`` — so its report is read from where it was really
    written instead of coming back empty for a collection that redacted via
    that job rather than its own run.
    """
    rows: dict[str, list[dict]] = {}
    for job in jobs:
        if job.is_active:
            continue
        report = reader(db, data_dir, (chained or {}).get(job.id, job.id))
        if report is not None:
            rows[job.id] = report_rows(report)
    return rows


def _chained_redactions(db, data_dir, jobs) -> dict[str, str]:
    """Collection id → the ``redact_artifact`` job that actually redacted it.

    Only for a collection with no report of its own — the common case (a
    collection that redacted as part of its own run) needs no lookup and gets
    none. For the rest, this is the same rules-aware lookup the Jobs page
    uses to refuse re-redacting a file: it finds a prior successful
    redaction of the collection's raw bundle, whichever job produced it —
    the Collect page's own "Redact now" button, or a support package build.
    """
    enabled = enabled_rules(db)
    found: dict[str, str] = {}
    for job in jobs:
        if job.is_active or report_from_job(db, data_dir, job.id) is not None:
            continue
        raw_bundle = next(
            (item for item in list_for_job(db, job.id) if item.name.endswith("-logs.tgz")), None
        )
        if raw_bundle is None:
            continue
        earlier = existing_redaction(db, data_dir, raw_bundle.id, enabled)
        if earlier is not None:
            found[job.id] = earlier.id
    return found


def _raw_bundles(artifacts: dict) -> dict:
    """Each collection's raw ``-logs.tgz``, keyed by job id, or absent.

    Computed here rather than in the template: Jinja's default test set has no
    regex ``match``, and a suffix check belongs beside the naming convention it
    depends on (``job_collect.LOGS_SUFFIX``-derived names), not duplicated in
    template markup.
    """
    return {
        job_id: next((item for item in produced if item.name.endswith("-logs.tgz")), None)
        for job_id, produced in artifacts.items()
    }


def _valid_category_keys(submitted: list[str]) -> list[str]:
    """The submitted category keys that are actually real ones, in display order.

    A checkbox list only ever sends keys this page itself rendered, but the
    job body validates independently too — this exists so a request with
    nothing valid in it is refused here, on the page, rather than becoming a
    job that immediately fails with "No log category was selected."
    """
    valid = {category.key for category in CATEGORIES}
    seen = {key for key in submitted if key in valid}
    return [category.key for category in CATEGORIES if category.key in seen]


def _extractions_by_source(db, extractions) -> dict[str, list]:
    """Extraction jobs grouped by the collection they were drawn from.

    An extraction started from an existing collection records it as
    ``artifact_id``; one queued alongside a fresh collection records
    ``source_job_id`` instead — see ``job_extract.run`` for why. Either way the
    collect page needs "which collection does this extraction belong to" to
    nest it under that collection's card, so both forms are resolved to the
    collection job id here, in one place, rather than in the template.
    """
    grouped: dict[str, list] = {}
    for job in extractions:
        source_job_id = job.params.get("source_job_id")
        if not isinstance(source_job_id, str) or not source_job_id:
            artifact_id = job.params.get("artifact_id")
            source = get_artifact(db, artifact_id) if isinstance(artifact_id, str) else None
            source_job_id = source.job_id if source is not None else None
        if source_job_id:
            grouped.setdefault(source_job_id, []).append(job)
    return grouped
