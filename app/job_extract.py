"""The "Extract categories" job: pull selected log families out of a bundle.

Xen Orchestra's log routes take no category filter and no date range — the
whole point of ``logs.tgz`` is that XCP-ng builds it once, server-side, as one
opaque archive (measured: 609 files, all of ``/var/log``, on a real host). A
request for "just the storage logs" can only be answered locally, against a
bundle already on the data volume, which is why this job takes a stored
artifact rather than a host: there is no second download to make it from.

It reuses ``job_collect._redact_tarball`` rather than repeating the
streaming/salvage/masking loop a second time: a category extraction and the
full redacted copy a collection makes are the same operation — read a tarball
member by member, mask the text, write what belongs in the output — with a
different answer to which members belong. The category filter is the only new
part.

Redaction is always the rules active *now*, read fresh at the moment this job
runs, the same as a fresh collection would use. There is no per-extraction rule
picker: changing what gets masked means changing the rules on the Redaction
page first, then extracting.
"""

from __future__ import annotations

import time
from typing import Any

from app.artifacts import Artifact, get_artifact, human_bytes, list_for_job, read_json, store_json
from app.job_collect import KIND as COLLECT_KIND
from app.job_collect import _redact_tarball
from app.job_redact import build_report as _redact_report
from app.job_runner import register
from app.jobs import CANCELLED, FAILED, JobContext, get_job
from app.log_categories import CATEGORIES, canonical_name, category_by_key, classify
from app.redact import enabled_rules

KIND = "extract_categories"

REPORT_ARTIFACT = "extraction-report.json"


def run(context: JobContext) -> None:
    """Extract the selected categories from one stored raw bundle.

    Params: either ``artifact_id`` (the raw ``*-logs.tgz`` to read from
    directly — extracting from an already-stored collection) or
    ``source_job_id`` (a collection job to read its raw bundle from once that
    job has produced one — extracting right after a fresh collection, queued
    in the same request as the collection itself). ``categories`` (a list of
    category keys) and ``include_rotated`` (bool, default off) apply either
    way. Raises rather than catching: the runner records the message against
    the job.

    ``source_job_id`` exists because a collection's raw bundle does not exist
    yet at the moment "collect, then extract" is queued — there is no artifact
    id to hand this job until the collection has actually run. The queue is
    FIFO and the worker runs one job at a time, so by the time this job is
    claimed, a collection enqueued first is guaranteed to have finished —
    successfully or not, which is why a failed source job is reported here
    rather than assumed.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}

    keys = _valid_categories(params.get("categories"))
    if not keys:
        raise ValueError("No log category was selected.")

    include_rotated = bool(params.get("include_rotated"))

    source = _resolve_source(context, params)
    _check_source(context, source)

    context.progress(5, f"Reading {source.name}")
    enabled = enabled_rules(context.conn)
    counts: dict[str, int] = {}
    matched: dict[str, int] = {key: 0 for key in keys}
    # Memoised per canonical name for this run: a 609-member bundle has far
    # fewer distinct names than members (32 rotations of the same log each),
    # so classifying by canonical name once each avoids repeating the prefix
    # scan in `classify` for every rotation.
    category_cache: dict[str, str] = {}
    # The selected categories' own directory-style prefixes ("blktap/",
    # "openvswitch/", ...), stripped of their trailing "/". `classify` decides
    # whether a *file* belongs under one of these; a directory entry's own
    # canonical name is the directory itself (tarfile strips the trailing
    # slash), which `classify` was never written to match — it would compare
    # "blktap" against the prefix "blktap/" and always lose. A directory
    # belongs in the output when it *is* one of these, so the files under it
    # keep a parent to live in.
    selected_dirs = {
        prefix.rstrip("/")
        for category in CATEGORIES
        if category.key in keys
        for prefix in category.prefixes
        if prefix.endswith("/")
    }

    def member_filter(member) -> bool:
        if not member.isfile():
            # A directory or symlink has no rotation history and is never
            # counted as a matched file, but it is still kept when it names a
            # selected category's own directory — `_redact_tarball` preserves
            # its metadata verbatim (job_collect.py) — so an extracted archive
            # keeps the same folder layout as the full bundle instead of a
            # flat pile of files with no parent directories.
            return canonical_name(member.name) in selected_dirs
        if not include_rotated and _member_is_rotated(member.name):
            return False
        name = canonical_name(member.name)
        key = category_cache.get(name)
        if key is None:
            key = classify(name)
            category_cache[name] = key
        if key not in keys:
            return False
        matched[key] += 1
        return True

    extracted = _redact_tarball(
        context,
        source,
        enabled,
        counts,
        member_filter=member_filter,
        working_name="extracting.tmp",
        store_name=_output_name(source.name, keys),
        progress_band=(10, 90),
        progress_step="Extracting selected categories",
    )

    context.progress(95, "Writing the report")
    report = build_report(
        source=source,
        extracted=extracted,
        categories=keys,
        matched=matched,
        include_rotated=include_rotated,
        enabled=enabled,
        counts=counts,
    )
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=REPORT_ARTIFACT,
        payload=report,
    )

    total_files = sum(matched.values())
    if total_files == 0:
        # Not an error — a real category can simply have nothing in this
        # bundle, e.g. a pool that never triggered HA. The empty archive is
        # still stored, and the report says why it is empty, so extracting the
        # same selection again does not look like it silently failed the same
        # way twice.
        context.progress(
            100,
            f"No files matched the selected categories in {source.name}",
        )
        return

    context.progress(
        100,
        f"{total_files} file(s), {human_bytes(extracted.size_bytes)} extracted from {source.name}",
    )


def _resolve_source(context: JobContext, params: dict) -> Artifact:
    """The raw bundle this run reads from, by either param a caller may give.

    ``artifact_id`` is resolved directly. ``source_job_id`` looks up that job's
    stored ``-logs.tgz`` instead — see ``run``'s docstring for why a job id is
    accepted at all here. Exactly one of the two is expected; both absent is a
    caller error, and both raise the same way a missing artifact does so a
    stale link and a bad id read the same on the page.
    """
    artifact_id = params.get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id:
        source = get_artifact(context.conn, artifact_id)
        if source is None:
            raise ValueError(f"Artifact {artifact_id} is no longer stored.")
        return source

    source_job_id = params.get("source_job_id")
    if isinstance(source_job_id, str) and source_job_id:
        source_job = get_job(context.conn, source_job_id)
        if source_job is None:
            raise ValueError("The collection this extraction was queued after no longer exists.")
        if source_job.state == FAILED:
            raise ValueError(
                "The collection this extraction was queued after failed, so there is "
                "no bundle to extract from."
            )
        if source_job.state == CANCELLED:
            raise ValueError(
                "The collection this extraction was queued after was cancelled, so "
                "there is no bundle to extract from."
            )
        if source_job.is_active:
            # The FIFO queue and single-worker run guarantee this cannot
            # actually happen — the source job is enqueued first and this one
            # is never claimed before it finishes — but failing loudly here
            # beats extracting from a bundle that is still being written to.
            raise ValueError(
                "The collection this extraction was queued after has not finished yet."
            )
        produced = list_for_job(context.conn, source_job_id)
        bundle = next((item for item in produced if item.name.endswith("-logs.tgz")), None)
        if bundle is None:
            raise ValueError("That collection produced no log bundle to extract from.")
        return bundle

    raise ValueError("No log bundle was named to extract from.")


def _check_source(context: JobContext, source: Artifact) -> None:
    """Refuse anything that is not a raw collected bundle.

    Extracting from a redacted copy would mask twice — harmless for most rules
    but wrong for a report claiming what was masked — and extracting from
    something that was never a collected bundle at all is a different job
    given the wrong artifact id by hand-edited params or a stale link.
    """
    job = get_job(context.conn, source.job_id)
    if job is None or job.kind != COLLECT_KIND or not source.name.endswith("-logs.tgz"):
        raise ValueError(
            f"{source.name} is not a raw collected log bundle. Extraction reads "
            f"from the raw '-logs.tgz' a collection stored, not a redacted or "
            f"unrelated file."
        )


def _valid_categories(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    valid = {category.key for category in CATEGORIES}
    # Order follows CATEGORIES, not the request, so the report and the output
    # name are stable regardless of what order a form happened to submit them.
    seen = {key for key in raw if isinstance(key, str) and key in valid}
    return [category.key for category in CATEGORIES if category.key in seen]


def _member_is_rotated(member_name: str) -> bool:
    """True when a member's own path names it as rotated history.

    A rotated log is either gzipped (``xensource.log.12.gz``) or has a bare
    numeric generation with no ``.gz`` (``xensource.log.31`` was measured
    alongside the ``.gz`` ones in a real bundle) — anything whose final
    dot-segment is purely digits, or is ``gz`` preceded by one that is.
    """
    parts = member_name.split(".")
    if len(parts) < 2:
        return False
    last = parts[-1]
    if last == "gz" and len(parts) >= 3:
        last = parts[-2]
    return last.isdigit()


def _output_name(source_name: str, keys: list[str]) -> str:
    """The extracted archive's file name.

    Prefixed with the host name already in the source's name (``host1-logs.tgz``
    becomes ``host1-<categories>.tgz``) so an operator who extracts from several
    hosts' collections still gets distinguishable filenames.
    """
    stem = source_name[: -len("-logs.tgz")] if source_name.endswith("-logs.tgz") else source_name
    if len(keys) <= 3:
        label = "-".join(keys)
    else:
        label = f"{len(keys)}-categories"
    return f"{stem}-{label}.tgz"


def build_report(
    *,
    source: Artifact,
    extracted: Artifact,
    categories: list[str],
    matched: dict[str, int],
    include_rotated: bool,
    enabled,
    counts: dict[str, int],
) -> dict[str, Any]:
    """The extraction's report as plain JSON — same shape family as a redaction
    report, so the same rule table renders on the Collect/Jobs pages.
    """
    template = _redact_report(
        source=source,
        redacted=extracted,
        enabled=enabled,
        counts=counts,
        lines=0,
    )

    return {
        **template,
        "categories": [
            {
                "key": key,
                "title": category_by_key(key).title if category_by_key(key) else key,
                "files": matched.get(key, 0),
            }
            for key in categories
        ],
        "include_rotated": include_rotated,
        "created_at": time.time(),
    }


def report_from_job(conn, data_dir, job_id: str) -> dict[str, Any] | None:
    """Read back a stored extraction report, or None."""
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == REPORT_ARTIFACT),
        None,
    )
    if artifact is None:
        return None
    try:
        payload = read_json(data_dir, artifact)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


register(KIND, run)
