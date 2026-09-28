"""The Support Package page: one archive ready to attach to a Vates ticket.

Two ways to start one: **Package** an already-stored collection, or
**Collect + Package** a host with nothing stored yet. Either way the result
is the same job chain — collect (skipped when reusing an existing bundle),
findings, inventory, then the package itself — because a package is never
allowed to ship with a gap it could have filled: see ``job_support_package``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app import retention
from app.activity import log_activity
from app.artifacts import get_artifact, human_bytes, list_for_job
from app.dependencies import (
    login_required,
    operator_required,
    redirect,
    serve_artifact,
    templates,
    wake_worker,
)
from app.job_collect import KIND as COLLECT_KIND
from app.job_extract import KIND as EXTRACT_KIND
from app.job_findings import KIND as FINDINGS_KIND
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_inventory import known_inventory
from app.job_redact import KIND as REDACT_KIND
from app.job_redact import existing_redaction
from app.job_support_package import KIND as SUPPORT_PACKAGE_KIND
from app.job_support_package import package_from_job
from app.jobs import SUCCEEDED, enqueue, get_job, has_active, list_jobs
from app.log_categories import category_keys
from app.redact import enabled_rules
from app.security import client_ip
from app.xo_connection import get_connection

router = APIRouter()
log = logging.getLogger("xcp_pulse.support_package")

# A package is a large, individually acted-on thing, not a stream of events —
# same reasoning as the Collect page's own PAGE_LIMIT.
PAGE_LIMIT = 25

# A ticked-host-checkbox list arrives as zero or more repeated form fields —
# the same convention the Collect page's own category checkboxes use.
_HOST_IDS_FIELD = Form(default_factory=list)


@router.get("/support-package", response_class=HTMLResponse)
def support_package_page(
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    connection = get_connection(db)
    inventory = known_inventory(db, data_dir)
    # Active collections are shown too, not just succeeded ones — otherwise a
    # host just started via Collect + Package has no card at all to show the
    # collection's own progress on, and the page looks like nothing happened
    # until the collection finishes minutes later.
    all_collections = list_jobs(db, kind=COLLECT_KIND, limit=PAGE_LIMIT)
    collections = [job for job in all_collections if job.state == SUCCEEDED or job.is_active]
    packages = list_jobs(db, kind=SUPPORT_PACKAGE_KIND, limit=PAGE_LIMIT)
    artifacts = {job.id: list_for_job(db, job.id) for job in packages}

    # A combined, multi-host package (built by ticking "one combined package"
    # on the Collect + Package card) has no single collection to nest under —
    # it is never grouped by ``_packages_by_source`` below and never counted
    # as orphaned either, since it was never tied to one collection's card in
    # the first place. It gets its own section on the page instead.
    pool_packages = [job for job in packages if isinstance(job.params.get("source_job_ids"), list)]
    pool_package_ids = {job.id for job in pool_packages}
    single_host_packages = [job for job in packages if job.id not in pool_package_ids]
    packages_by_source = _packages_by_source(single_host_packages)

    # A package outlives the collection it was built from — the archive is
    # self-contained and does not need the raw bundle to exist any more — but
    # the Collect page's own delete button does not know about packages and
    # will happily remove a collection with a finished package still nested
    # under it. Without this, that package would still be on disk and in the
    # database, still downloadable by direct link, and simply never listed
    # here again: nested only under a collection card that no longer exists.
    known_collection_ids = {job.id for job in collections}
    orphaned_packages = [
        job
        for job in single_host_packages
        if job.params.get("source_job_id") not in known_collection_ids
    ]

    # Findings and inventory runs a package chain queued are invisible on this
    # page — only the collection and the package job itself get cards — so
    # without checking them too, the refresh meta tag stops firing the moment
    # the collection finishes and the page sits still while those two jobs
    # (and the package waiting on them) are still running.
    any_active = (
        any(job.is_active for job in collections)
        or any(job.is_active for job in packages)
        or has_active(db, FINDINGS_KIND)
        or has_active(db, INVENTORY_KIND)
    )

    return templates.TemplateResponse(
        request,
        "support_package.html",
        {
            "username": username,
            "connection": connection,
            "hosts": sorted(inventory.hosts, key=lambda host: host.name.lower()),
            "collections": collections,
            "packages": packages,
            "packages_by_source": packages_by_source,
            "orphaned_packages": orphaned_packages,
            "pool_packages": pool_packages,
            "artifacts": artifacts,
            "human_bytes": human_bytes,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "any_active": any_active,
        },
    )


@router.post("/support-package/{job_id}/package")
def package_collection(
    job_id: str,
    request: Request,
    username: str = Depends(operator_required),
    date_preset: str = Form(default=""),
    date_start: str = Form(default=""),
    date_end: str = Form(default=""),
) -> Response:
    """Package an already-stored collection.

    ``job_id`` names the collection, the same way the Collect page's extract
    button is keyed by the collection rather than its bundle — checked eagerly
    here so a deleted collection is a message on this page, not a job the
    operator has to go and read to understand.

    A collection stored with redaction switched off, or from before that
    checkbox existed, has no redaction report of its own — a package is never
    allowed to ship that gap, so one is queued into the same chain rather
    than failing the package with "nothing to package" for a choice the
    operator made deliberately.

    That check has to look for *any* prior redaction of the collection's raw
    bundle, not only one produced by the collection job itself: the "Redact
    now" button on the Collect page, or an earlier build of this same
    package, both redact via a separate ``redact_artifact`` job whose report
    lives under that job's own id. Reading only the collection's own id would
    call the bundle unredacted forever and re-redact it — 433 MB and a couple
    of minutes — on every single package built from it.

    A date range (any of the three fields non-blank) skips that redaction
    check entirely and instead chains a date-filtered ``extract_categories``
    job — every category, narrowed to the range — ahead of the package. See
    ``job_support_package.run`` for why the extraction's own report and
    bundle stand in for the collection's full redacted copy.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    collection = get_job(db, job_id)
    if collection is None or collection.kind != COLLECT_KIND:
        return redirect("/support-package?error=That+collection+is+no+longer+stored.")

    if _busy(db):
        return redirect("/support-package?notice=A+collection+or+package+is+already+running.")

    raw_bundle = next(
        (item for item in list_for_job(db, job_id) if item.name.endswith("-logs.tgz")), None
    )

    if date_preset or date_start or date_end:
        if raw_bundle is None:
            return redirect(
                "/support-package?error=That+collection+has+no+log+bundle+to+package+from."
            )
        extract_job = enqueue(
            db,
            EXTRACT_KIND,
            {
                "artifact_id": raw_bundle.id,
                "categories": list(category_keys()),
                "include_rotated": True,
                "date_preset": date_preset,
                "date_start": date_start,
                "date_end": date_end,
            },
        )
        _enqueue_chain(request, source_job_id=job_id, extract_job_id=extract_job.id)
        log.info(
            "queued %s for existing collection %s by %s, date-filtered",
            SUPPORT_PACKAGE_KIND,
            job_id,
            username,
        )
        log_activity(db, username, "package.start", detail=job_id, ip=client_ip(request))
        return redirect("/support-package?notice=Building+the+support+package.")

    redact_source_job_id = None
    if raw_bundle is not None:
        earlier = existing_redaction(db, data_dir, raw_bundle.id, enabled_rules(db))
        if earlier is None:
            redact_source_job_id = job_id

    _enqueue_chain(
        request,
        source_job_id=job_id,
        redact_source_job_id=redact_source_job_id,
    )
    log.info("queued %s for existing collection %s by %s", SUPPORT_PACKAGE_KIND, job_id, username)
    log_activity(db, username, "package.start", detail=job_id, ip=client_ip(request))
    return redirect("/support-package?notice=Building+the+support+package.")


@router.post("/support-package/collect")
def collect_and_package(
    request: Request,
    username: str = Depends(operator_required),
    host_ids: list[str] = _HOST_IDS_FIELD,
    combine: str = Form(default=""),
    include_audit: str = Form(default=""),
    date_preset: str = Form(default=""),
    date_start: str = Form(default=""),
    date_end: str = Form(default=""),
) -> Response:
    """Collect one or more hosts' logs, then package the result.

    For hosts with nothing stored yet. Every ticked host gets its own fresh
    ``collect_logs`` job, chained ahead of the same findings/inventory/package
    sequence ``package_collection`` queues, addressed by ``source_job_id`` the
    same way the Collect page chains an extraction behind a fresh collection.

    ``combine`` picks what a support ticket usually wants when more than one
    host is ticked — a support ticket is normally about one pool-wide
    incident, not one host in isolation, so shipping the operator N separate
    archives means they have to remember to attach all of them. When ticked
    (and more than one host is selected), every collection feeds into one
    ``support_package`` job addressed by ``source_job_ids`` (see
    ``job_support_package._run_pool``) instead of a separate chain per host.
    Left off, or with only one host ticked, behaves exactly as before more
    than one host could be selected: one chain, one archive, per host.

    A date range chains a date-filtered ``extract_categories`` job per host
    too, addressed by ``source_job_id`` rather than an artifact id — the same
    "collect, then extract" path the Collect page already uses, since the
    collection here has not produced its raw bundle yet at enqueue time.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    if get_connection(db) is None:
        return redirect("/support-package?error=Configure+a+Xen+Orchestra+connection+first.")

    if _busy(db):
        return redirect("/support-package?notice=A+collection+or+package+is+already+running.")

    inventory = known_inventory(db, data_dir)
    by_id = {item.id: item for item in inventory.hosts}
    # ``dict.fromkeys`` drops a host ticked twice (a resubmit, say) while
    # keeping the order the page rendered them in.
    picked = [by_id[host_id] for host_id in dict.fromkeys(host_ids) if host_id in by_id]
    if not picked:
        return redirect("/support-package?error=Pick+at+least+one+host+to+collect+from.")

    has_date_range = bool(date_preset or date_start or date_end)
    collect_jobs = []
    for host in picked:
        collect_job = enqueue(
            db,
            COLLECT_KIND,
            {
                "host_id": host.id,
                "host_name": host.name,
                "include_audit": bool(include_audit),
                # A support package always needs a redacted bundle, regardless
                # of what the Collect page's own checkbox currently reads —
                # this route is not that form and never should silently
                # inherit its state.
                "redact": True,
            },
        )
        log.info(
            "queued %s job %s for host %s by %s", COLLECT_KIND, collect_job.id, host.name, username
        )
        collect_jobs.append(collect_job)

    extract_job_ids: list[str | None] = []
    for collect_job in collect_jobs:
        extract_job_id = None
        if has_date_range:
            extract_job = enqueue(
                db,
                EXTRACT_KIND,
                {
                    "source_job_id": collect_job.id,
                    "categories": list(category_keys()),
                    "include_rotated": True,
                    "date_preset": date_preset,
                    "date_start": date_start,
                    "date_end": date_end,
                },
            )
            extract_job_id = extract_job.id
        extract_job_ids.append(extract_job_id)

    names = ", ".join(host.name for host in picked)
    log_activity(db, username, "collect_and_package.start", detail=names, ip=client_ip(request))

    if bool(combine) and len(picked) > 1:
        _enqueue_chain(
            request,
            source_job_ids=[job.id for job in collect_jobs],
            extract_job_ids=extract_job_ids if has_date_range else None,
        )
        return redirect(
            f"/support-package?notice=Collecting+from+{len(picked)}+hosts+and+"
            "building+one+combined+package."
        )

    for collect_job, extract_job_id in zip(collect_jobs, extract_job_ids, strict=True):
        _enqueue_chain(request, source_job_id=collect_job.id, extract_job_id=extract_job_id)

    if len(picked) == 1:
        return redirect(
            f"/support-package?notice=Collecting+from+{picked[0].name}+and+building+the+package."
        )
    return redirect(
        f"/support-package?notice=Collecting+from+{len(picked)}+hosts+and+"
        f"building+{len(picked)}+packages."
    )


@router.get("/support-package/download/{artifact_id}")
def download_package(
    artifact_id: str,
    request: Request,
    username: str = Depends(login_required),
) -> Response:
    """Serve one stored support package as a download."""
    db = request.app.state.db
    artifact = get_artifact(db, artifact_id)
    if artifact is not None:
        log.info("%s downloaded %s (%s)", username, artifact.name, artifact.size_human)
        log_activity(db, username, "artifact.download", detail=artifact.name, ip=client_ip(request))
    return serve_artifact(request, artifact_id, on_error="/support-package")


@router.post("/support-package/{job_id}/delete")
def delete_package(
    job_id: str,
    request: Request,
    username: str = Depends(operator_required),
) -> Response:
    """Delete one support package and the archive it produced."""
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    package = package_from_job(db, job_id)
    if retention.delete_job(db, data_dir, job_id, kind=SUPPORT_PACKAGE_KIND):
        name = package.name if package is not None else job_id
        log.info("%s deleted support package %s", username, name)
        log_activity(db, username, "package.delete", detail=name, ip=client_ip(request))
        return redirect("/support-package?notice=Support+package+deleted.")
    return redirect("/support-package?error=There+is+no+such+package+to+delete.")


def _busy(db) -> bool:
    """True when a package would compete with something already running.

    Mirrors the Collect page's own check: a package always starts with (or
    reuses) a collection, and the worker runs one job at a time regardless, so
    queuing a second chain here would only sit behind the first, looking stuck.

    Also checks the two kinds ``_enqueue_chain`` queues as part of the same
    chain, not only the collection and the package job itself: a findings run
    started from the Findings page, or an inventory refresh started from the
    dashboard, is still a job this chain would queue a duplicate of — the
    single worker thread runs one job at a time regardless of which page
    started it. A redaction started from the Jobs page is checked too, for
    the same reason.
    """
    return (
        has_active(db, COLLECT_KIND)
        or has_active(db, SUPPORT_PACKAGE_KIND)
        or has_active(db, FINDINGS_KIND)
        or has_active(db, INVENTORY_KIND)
        or has_active(db, REDACT_KIND)
        or has_active(db, EXTRACT_KIND)
    )


def _enqueue_chain(
    request: Request,
    *,
    source_job_id: str | None = None,
    source_job_ids: list[str] | None = None,
    redact_source_job_id: str | None = None,
    extract_job_id: str | None = None,
    extract_job_ids: list[str | None] | None = None,
) -> None:
    """Queue whichever of findings/inventory/redaction the package needs, then
    the package itself.

    A package never ships with a gap it could have filled — see
    ``job_support_package`` — so this always queues a fresh findings run and a
    fresh inventory refresh alongside the collection(s), rather than reusing
    whatever last happened to be stored. Each is addressed by
    ``source_job_id``/``findings_job_id``/``inventory_job_id`` rather than an
    artifact id, because none of their artifacts exist yet at enqueue time —
    the FIFO queue and single worker guarantee every job here has finished,
    successfully or not, by the time the package job is claimed.

    Exactly one of ``source_job_id``/``source_job_ids`` is given. The plural
    form queues one ``support_package`` job covering every collection listed —
    the "one combined package" choice on the Collect + Package card — read by
    ``job_support_package._run_pool`` instead of ``_run_single``; the caller
    is responsible for only passing it when more than one host was actually
    ticked, since a single-item list would otherwise take a different, less
    exercised code path for no reason.

    ``redact_source_job_id``, when given, is a collection with no redaction
    report yet — collected with the Collect page's redaction checkbox off, or
    stored before that checkbox existed — so a ``redact_artifact`` job is
    queued against it too, addressed by ``source_job_id`` the same way
    ``job_extract`` reads a collection's raw bundle once that collection has
    actually run. Only meaningful with the singular form: every combined-
    package collection is always collected with redaction on (see
    ``collect_and_package``), so there is never a gap to fill this way.

    ``extract_job_id``/``extract_job_ids``, when given, are date-filtered
    ``extract_categories`` job(s) the caller already enqueued (each needs a
    real raw bundle id up front, unlike the jobs above, so they are queued by
    the caller rather than here) — passed straight through as the package's
    own param. Never combined with ``redact_source_job_id``: a date range
    replaces the redaction check entirely, per ``package_collection``.
    """
    db = request.app.state.db

    findings_job = enqueue(db, FINDINGS_KIND, {})
    inventory_job = enqueue(db, INVENTORY_KIND, {})
    redact_job_id = None
    if redact_source_job_id is not None:
        redact_job = enqueue(db, REDACT_KIND, {"source_job_id": redact_source_job_id})
        redact_job_id = redact_job.id

    params: dict[str, Any] = {
        "findings_job_id": findings_job.id,
        "inventory_job_id": inventory_job.id,
        "redact_job_id": redact_job_id,
    }
    if source_job_ids is not None:
        params["source_job_ids"] = source_job_ids
        params["extract_job_ids"] = extract_job_ids
    else:
        params["source_job_id"] = source_job_id
        params["extract_job_id"] = extract_job_id

    enqueue(db, SUPPORT_PACKAGE_KIND, params)
    wake_worker(request)


def _packages_by_source(packages) -> dict[str, list]:
    """Support-package jobs grouped by the collection they were built from.

    So the page can nest each collection's packages under its card, the same
    way the Collect page nests extractions under theirs.
    """
    grouped: dict[str, list] = {}
    for job in packages:
        source_id = job.params.get("source_job_id")
        if isinstance(source_id, str) and source_id:
            grouped.setdefault(source_id, []).append(job)
    return grouped
