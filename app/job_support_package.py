"""The "Support package" job: one archive to attach to a Vates ticket.

An operator sending Vates a bundle today gathers several separate downloads by
hand — the redacted log bundle, the findings report, the redaction report, the
inventory — and has to remember all of them. This job assembles the same
material into one ``.tgz``.

**It never ships a gap it could have filled itself.** If the target host has
no findings run yet, or the inventory has never been refreshed, this job does
not package what exists and note the hole — it runs the missing piece first,
then packages a complete snapshot. That is why this job takes longer than the
files it reads: it is not read-only the way ``job_extract`` is, because a
support package always has to be current, not "whatever happened to be on
disk".

The worker is single-threaded and the queue is strict FIFO (see
``app/job_runner.py``), so this job cannot enqueue a sub-job and wait for it —
the worker is busy running *this* job, so nothing else can ever be claimed
while it waits. Chaining therefore happens the same way ``job_extract``
already chains behind a fresh collection: the route enqueues every job the
package needs, each one before the one that depends on it, addressed by
``source_job_id`` rather than an artifact id that does not exist yet. This
job is always the last in that chain, and resolves each ``source_job_id`` once
it runs — by which point every job queued ahead of it has already finished,
successfully or not.
"""

from __future__ import annotations

import json
import tarfile
import time
from typing import Any

from app.artifacts import (
    Artifact,
    artifact_path,
    list_for_job,
    store_file,
)
from app.job_collect import KIND as COLLECT_KIND
from app.job_extract import KIND as EXTRACT_KIND
from app.job_extract import report_from_job as extract_report_from_job
from app.job_findings import FINDINGS_ARTIFACT, FINDINGS_MARKDOWN
from app.job_findings import KIND as FINDINGS_KIND
from app.job_inventory import INVENTORY_ARTIFACT
from app.job_inventory import KIND as INVENTORY_KIND
from app.job_redact import KIND as REDACT_KIND
from app.job_redact import existing_redaction
from app.job_redact import report_from_job as redaction_report_from_job
from app.job_runner import register
from app.jobs import CANCELLED, FAILED, Job, JobContext, get_job
from app.redact import enabled_rules

KIND = "support_package"

MANIFEST_NAME = "manifest.json"
REDACTION_REPORT_NAME = "redaction-report.json"


def run(context: JobContext) -> None:
    """Assemble one support package from a collection and its two sibling jobs.

    Params: ``source_job_id`` names the ``collect_logs`` job whose bundle this
    packages. ``findings_job_id`` and ``inventory_job_id`` name the
    ``api_findings`` and ``refresh_inventory`` jobs queued alongside it — both
    always present, because the route that enqueues this job always enqueues
    whichever of the two was missing or stale first. All three are resolved
    here rather than passed as artifact ids, for the same reason
    ``job_extract`` resolves ``source_job_id``: a job queued in the same
    request as this one has not produced its artifacts yet at enqueue time.

    ``redact_job_id``, when present, names a ``redact_artifact`` job the route
    queued because the collection itself had no redaction anywhere yet —
    collecting with redaction switched off is now a choice, not only
    something an older stored collection could be missing, so a package built
    from either reads its redacted bundle and report from that job instead of
    demanding the collection have produced them itself. Absent covers two
    different cases the same way: the collection redacted itself (the common
    case), or a raw-only collection's bundle was already redacted earlier by
    some other job — the Collect page's own "Redact now" button, or a prior
    package build — found here by the same rules-aware lookup the Jobs page
    uses to refuse a duplicate redaction, so this job never re-redacts a 433
    MB bundle that a stored copy already answers for.

    ``extract_job_id``, when present, names an ``extract_categories`` job the
    route chained ahead of this one because a date range was given — every
    category, narrowed to the range, so the package ships a date-filtered
    bundle in place of the full redacted copy. Its own report already carries
    the same rule rows a redaction report does (``job_extract.build_report``
    reuses ``job_redact.build_report``'s shape), so it stands in for
    ``redaction_report`` below without a second code path. Absent means the
    common case: no date range was asked for, so the full redacted bundle
    ships, exactly as before this parameter existed.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}

    context.progress(5, "Checking the collection")
    collection = _resolve_job(context, params.get("source_job_id"), COLLECT_KIND, "collection")
    context.progress(15, "Checking the findings report")
    findings_job = _resolve_job(context, params.get("findings_job_id"), FINDINGS_KIND, "findings")
    context.progress(25, "Checking the inventory")
    inventory_job = _resolve_job(
        context, params.get("inventory_job_id"), INVENTORY_KIND, "inventory"
    )

    extract_job_id = params.get("extract_job_id")
    if isinstance(extract_job_id, str) and extract_job_id:
        context.progress(30, "Checking the date-filtered extraction")
        extraction = _resolve_job(context, extract_job_id, EXTRACT_KIND, "extraction")
        redaction_report = extract_report_from_job(context.conn, context.data_dir, extraction.id)
        if redaction_report is None:
            raise ValueError(f"Extraction {extraction.id} produced no report to package.")
        redacted_bundle = _find_artifact(context, extraction.id, suffix=".tgz")
    else:
        redact_job_id = params.get("redact_job_id")
        if isinstance(redact_job_id, str) and redact_job_id:
            context.progress(30, "Checking the redaction")
            redaction_source = _resolve_job(context, redact_job_id, REDACT_KIND, "redaction")
        else:
            redaction_source = _existing_redaction_source(context, collection)

        # Checked before looking for the bundle itself: a collection with
        # redaction switched off, no ``redact_job_id`` chained behind it, and
        # no earlier redaction found either has neither report nor bundle,
        # and "no redaction report" is the actionable message — the operator
        # needs to know to redact it, not that a file search came up empty.
        redaction_report = redaction_report_from_job(
            context.conn, context.data_dir, redaction_source.id
        )
        if redaction_report is None:
            raise ValueError(f"Collection {collection.id} has no redaction report to package.")
        redacted_bundle = _find_artifact(context, redaction_source.id, suffix=".redacted.")

    findings_json = _find_artifact(context, findings_job.id, name=FINDINGS_ARTIFACT)
    findings_md = _find_artifact(context, findings_job.id, name=FINDINGS_MARKDOWN)
    inventory_json = _find_artifact(context, inventory_job.id, name=INVENTORY_ARTIFACT)

    host_name = collection.params.get("host_name") or collection.params.get("host_id") or "host"

    context.progress(40, "Staging the files")
    staging = artifact_path(context.data_dir, context.job_id, "staging")
    staging.mkdir(parents=True, exist_ok=True)

    stored_entries = [
        (redacted_bundle, redacted_bundle.name),
        (findings_json, FINDINGS_ARTIFACT),
        (findings_md, FINDINGS_MARKDOWN),
        (inventory_json, INVENTORY_ARTIFACT),
    ]

    # Written fresh rather than read back as an artifact: it is already in
    # memory from the earlier check, and staging it alongside the manifest
    # keeps the "generated here" files together.
    report_path = staging / REDACTION_REPORT_NAME
    report_path.write_text(json.dumps(redaction_report, indent=2, sort_keys=True), encoding="utf-8")

    all_names = [name for _artifact, name in stored_entries] + [REDACTION_REPORT_NAME]
    manifest = build_manifest(
        host_name=str(host_name),
        collection=collection,
        redacted_bundle=redacted_bundle,
        redaction_report=redaction_report,
        findings_job=findings_job,
        inventory_job=inventory_job,
        entries=all_names,
    )
    manifest_path = staging / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    context.progress(60, "Building the archive")
    archive_path = artifact_path(context.data_dir, context.job_id, "package.tmp")
    with tarfile.open(archive_path, "w:gz") as archive:
        for artifact, arcname in stored_entries:
            source_path = artifact_path(context.data_dir, artifact.job_id, artifact.id)
            archive.add(source_path, arcname=arcname)
        archive.add(report_path, arcname=REDACTION_REPORT_NAME)
        archive.add(manifest_path, arcname=MANIFEST_NAME)

    context.progress(90, "Storing the package")
    package = store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=_package_name(str(host_name)),
        media_type="application/gzip",
        source=archive_path,
    )

    context.progress(100, f"{package.name} ready — {package.size_human}")


def _existing_redaction_source(context: JobContext, collection: Job) -> Job:
    """Where this collection's redaction report actually lives, if anywhere.

    The collection itself, when it redacted as part of its own run — still
    the common case. Failing that, the collection's raw bundle may already
    have been redacted by a separate ``redact_artifact`` job (the Collect
    page's "Redact now" button, or an earlier package build), found the same
    way the Jobs page finds a duplicate to refuse re-redacting one: by
    rules-aware report content, not by which job happened to produce it.
    Falling back to the collection itself when nothing is found keeps the
    caller's "no redaction report" error pointed at the collection, which is
    what the operator actually needs to act on.
    """
    if redaction_report_from_job(context.conn, context.data_dir, collection.id) is not None:
        return collection

    raw_bundle = next(
        (
            item
            for item in list_for_job(context.conn, collection.id)
            if item.name.endswith("-logs.tgz")
        ),
        None,
    )
    if raw_bundle is None:
        return collection

    earlier = existing_redaction(
        context.conn, context.data_dir, raw_bundle.id, enabled_rules(context.conn)
    )
    return earlier if earlier is not None else collection


def _resolve_job(context: JobContext, job_id: object, expected_kind: str, label: str) -> Job:
    """A finished, successful job of the expected kind, or raise plainly.

    Every source this job reads was queued ahead of it in the same FIFO
    request, so by the time this job is claimed each one has already run to
    completion — successfully or not. A failed or missing source is reported
    here rather than assumed, the same as ``job_extract._resolve_source``.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError(f"No {label} job was named for this package.")
    source = get_job(context.conn, job_id)
    if source is None:
        raise ValueError(f"The {label} job this package was queued after no longer exists.")
    if source.kind != expected_kind:
        raise ValueError(f"Job {job_id} is not a {label} job.")
    if source.state == FAILED:
        raise ValueError(f"The {label} job this package was queued after failed.")
    if source.state == CANCELLED:
        raise ValueError(f"The {label} job this package was queued after was cancelled.")
    if source.is_active:
        # Cannot actually happen given the FIFO queue and single worker — see
        # the module docstring — but failing loudly beats reading artifacts
        # that are still being written.
        raise ValueError(f"The {label} job this package was queued after has not finished yet.")
    return source


def _find_artifact(
    context: JobContext, job_id: str, *, name: str | None = None, suffix: str | None = None
) -> Artifact:
    """One artifact a job produced, by exact name or by a substring in its name.

    Exactly one of ``name``/``suffix`` is expected, the same convention
    ``job_extract._resolve_source`` uses for its own two-parameter lookup.
    """
    produced = list_for_job(context.conn, job_id)
    if name is not None:
        found = next((item for item in produced if item.name == name), None)
        wanted = name
    else:
        assert suffix is not None
        found = next((item for item in produced if suffix in item.name), None)
        wanted = f"file containing {suffix!r}"
    if found is None:
        raise ValueError(f"Job {job_id} produced no {wanted}.")
    return found


def _package_name(host_name: str) -> str:
    """The archive's file name: the host it covers, so several packages on the
    data volume are distinguishable without opening one."""
    return f"{host_name}-support-package.tgz"


def build_manifest(
    *,
    host_name: str,
    collection: Job,
    redacted_bundle: Artifact,
    redaction_report: dict[str, Any],
    findings_job: Job,
    inventory_job: Job,
    entries: list[str],
) -> dict[str, Any]:
    """What the archive contains and what was masked, as plain JSON.

    Vates, and the operator re-opening this months later, should not have to
    open every file to find out what is inside or whether redaction ran with
    every rule on — this states it up front. ``rules_disabled`` is lifted
    straight from the redaction report rather than re-derived, so the manifest
    can never disagree with the report sitting next to it in the same archive.

    ``date_start``/``date_end`` come the same way, from whichever report was
    packaged — an extraction's report carries them when a date range narrowed
    the bundle (see ``run``), and a full collection's redaction report always
    has both ``None``, which is what "the whole bundle, unfiltered" means
    here.
    """
    return {
        "host_name": host_name,
        "created_at": time.time(),
        "files": entries,
        "collection": {
            "job_id": collection.id,
            "collected_at": collection.finished_at,
        },
        "redacted_bundle": {
            "name": redacted_bundle.name,
            "size_bytes": redacted_bundle.size_bytes,
            "sha256": redacted_bundle.sha256,
        },
        "rules_disabled": redaction_report.get("rules_disabled", []),
        "date_start": redaction_report.get("date_start"),
        "date_end": redaction_report.get("date_end"),
        "findings_job_id": findings_job.id,
        "inventory_job_id": inventory_job.id,
    }


def package_from_job(conn, job_id: str) -> Artifact | None:
    """The archive a completed support-package job stored, or None."""
    produced = list_for_job(conn, job_id)
    return next((item for item in produced if item.name.endswith("-support-package.tgz")), None)


register(KIND, run)
