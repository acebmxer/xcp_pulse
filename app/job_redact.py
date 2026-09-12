"""The "Redact artifact" job: mask a stored file and record what was masked.

The preview page answers "what would this do?" for a paste. This answers it for
a real file, and leaves the answer behind: a redacted copy of the artifact, and
a report artifact holding the per-rule hit counts for the whole run.

**It reads a line at a time and never holds the file.** That is the point of
building it here rather than inside collection: the same loop that redacts a
few hundred bytes of ``inventory.json`` today redacts a 433 MB log bundle
later, because at no moment does more than one line exist in memory.

The report is an artifact rather than a column on the job, because it is a
result — it is produced by the run, belongs to it, and is deleted with it.
"""

from __future__ import annotations

import time
from typing import Any

from app.artifacts import (
    Artifact,
    artifact_path,
    get_artifact,
    list_for_job,
    read_json,
    store_file,
    store_json,
)
from app.job_runner import register
from app.jobs import CANCELLED, FAILED, SUCCEEDED, Job, JobContext, get_job, list_jobs
from app.redact import RULES, active_rules, enabled_rules, rule_by_name

KIND = "redact_artifact"

# The report this job writes. One name, used to write it and to find it again,
# so the reader cannot drift from the writer.
REPORT_ARTIFACT = "redaction-report.json"

# What a redacted copy is called: the source name with this before its suffix,
# so `xensource.log` becomes `xensource.redacted.log` and stays openable by
# whatever opens the original.
REDACTED_MARKER = "redacted"

# How often the line loop reports progress. Every line would be a database
# write per line, which on a 433 MB bundle is millions of them for a bar that
# moves in whole percent.
_PROGRESS_EVERY_LINES = 5000


def redacted_name(name: str) -> str:
    """The name a redacted copy of ``name`` is stored under.

    The suffix is kept last so the file still opens as what it is: a redacted
    ``.log`` is a ``.log``, not a ``.redacted``.
    """
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return f"{name}.{REDACTED_MARKER}"
    return f"{stem}.{REDACTED_MARKER}.{suffix}"


# How far back the duplicate check looks. A redaction store deep enough to hold
# more than this has long since been thinned by retention.
_DUPLICATE_SCAN_LIMIT = 200


def existing_redaction(
    conn,
    data_dir,
    artifact_id: str,
    enabled,
) -> Job | None:
    """A finished redaction of this file with these same rules, or None.

    Redacting the same artifact twice with the same rules switched on produces
    a byte-identical copy and an identical report: two files, no new answer,
    and a data volume the size of the bundle spent on it. A *different* set of
    rules is a different result and is allowed.

    Returns the job rather than its id so a caller refusing a duplicate can say
    *which* run it means — when it ran, and what it left behind. A refusal
    naming nothing sends the operator to scroll the history to find out whether
    the earlier copy is even still stored.

    Read from the stored reports rather than a new table: the report already
    records the source artifact id and each rule's enabled state, so what is
    needed to answer this is on disk already.
    """
    wanted = set(enabled)
    for job in list_jobs(conn, kind=KIND, limit=_DUPLICATE_SCAN_LIMIT):
        if job.state != SUCCEEDED:
            continue
        report = report_from_job(conn, data_dir, job.id)
        if report is None:
            continue
        source = report.get("source")
        if not isinstance(source, dict) or source.get("artifact_id") != artifact_id:
            continue
        if _report_enabled(report) == wanted:
            return job
    return None


def _report_enabled(report: dict[str, Any]) -> set[str]:
    """The rule names a stored report says were switched on for that run."""
    return {
        row["name"]
        for row in report.get("rules", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row.get("enabled")
    }


def run(context: JobContext) -> None:
    """Redact one stored artifact, storing the result and a report.

    Params: either ``artifact_id`` (the stored file to redact directly) or
    ``source_job_id`` (a collection job to read its raw ``-logs.tgz`` from once
    that job has produced one — chained behind a fresh, redact-later
    collection the same way ``job_extract`` chains behind one). Raises rather
    than catching: the runner records the message against the job, which is
    where the operator looks for it.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}
    source = _resolve_source(context, params)

    context.progress(5, f"Reading {source.name}")
    source_path = artifact_path(context.data_dir, source.job_id, source.id)
    if not source_path.is_file():
        raise ValueError(f"The body of {source.name} is missing from the data volume.")

    enabled = enabled_rules(context.conn)
    working = artifact_path(context.data_dir, context.job_id, "redacting.tmp")
    working.parent.mkdir(parents=True, exist_ok=True)

    counts, lines = _redact_file(context, source_path, working, source.size_bytes, enabled)

    context.progress(90, "Storing the redacted copy")
    redacted = store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=redacted_name(source.name),
        media_type=source.media_type,
        source=working,
    )

    context.progress(95, "Writing the report")
    report = build_report(
        source=source,
        redacted=redacted,
        enabled=enabled,
        counts=counts,
        lines=lines,
    )
    _store_report(context, report)

    total = report["total_hits"]
    context.progress(100, f"{total} value(s) masked in {lines} line(s)")


def _resolve_source(context: JobContext, params: dict) -> Artifact:
    """The artifact this run redacts, by either param a caller may give.

    ``artifact_id`` is resolved directly. ``source_job_id`` looks up that
    job's stored raw ``-logs.tgz`` instead — for a collection queued with
    redaction switched off, whose bundle does not exist yet at the moment the
    redaction is queued behind it in the same request. Exactly one of the two
    is expected; both absent is a caller error.
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
            raise ValueError("The collection this redaction was queued after no longer exists.")
        if source_job.state == FAILED:
            raise ValueError(
                "The collection this redaction was queued after failed, so there is "
                "no bundle to redact."
            )
        if source_job.state == CANCELLED:
            raise ValueError(
                "The collection this redaction was queued after was cancelled, so "
                "there is no bundle to redact."
            )
        if source_job.is_active:
            # The FIFO queue and single-worker run guarantee this cannot
            # actually happen — the source job is enqueued first and this one
            # is never claimed before it finishes — but failing loudly here
            # beats redacting a bundle that is still being written to.
            raise ValueError("The collection this redaction was queued after has not finished yet.")
        produced = list_for_job(context.conn, source_job_id)
        bundle = next((item for item in produced if item.name.endswith("-logs.tgz")), None)
        if bundle is None:
            raise ValueError("That collection produced no log bundle to redact.")
        return bundle

    raise ValueError("No artifact was named to redact.")


def _redact_file(
    context: JobContext,
    source_path,
    destination_path,
    source_bytes: int,
    enabled,
) -> tuple[dict[str, int], int]:
    """Mask every line of one file into another. Returns counts and line count.

    This is deliberately not ``redact_text``, because that takes and returns a
    whole string and the file this must eventually handle is 433 MB. The
    masking itself is the same code: ``active_rules`` and ``Rule.apply``, in
    the same order, so the preview and a real run can never mask differently.
    Do not add a third loop over the rules — extend one of these two.
    """
    rules = active_rules(enabled)
    counts: dict[str, int] = {}
    lines = 0
    read_bytes = 0

    # errors="surrogateescape" so a log with one malformed byte is redacted
    # rather than failing the run: the byte survives the round trip untouched.
    with (
        source_path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as reader,
        destination_path.open("w", encoding="utf-8", errors="surrogateescape", newline="") as out,
    ):
        for line in reader:
            for rule in rules:
                line, hits = rule.apply(line)
                if hits:
                    counts[rule.name] = counts.get(rule.name, 0) + hits
            out.write(line)
            lines += 1
            read_bytes += len(line)

            if lines % _PROGRESS_EVERY_LINES == 0:
                # 5–85% spans the read, leaving room either side for the store
                # and the report. A zero-byte source would divide by zero.
                share = read_bytes / source_bytes if source_bytes else 1.0
                context.progress(5 + int(min(1.0, share) * 80), f"Redacting — {lines} lines")

    return counts, lines


def build_report(
    *,
    source: Artifact,
    redacted: Artifact,
    enabled,
    counts: dict[str, int],
    lines: int,
) -> dict[str, Any]:
    """The report as plain JSON.

    Every rule appears, including the ones that matched nothing and the ones
    that were switched off — a report that lists only what fired cannot answer
    "was this masked?", which is the question someone about to send a bundle
    to Vates is actually asking.
    """
    rules = []
    for rule in RULES:
        rules.append(
            {
                "name": rule.name,
                "title": rule.title,
                "placeholder": rule.placeholder,
                "enabled": rule.name in enabled,
                "hits": counts.get(rule.name, 0),
            }
        )

    return {
        "source": {
            "artifact_id": source.id,
            "name": source.name,
            "size_bytes": source.size_bytes,
            "sha256": source.sha256,
        },
        "redacted": {
            "artifact_id": redacted.id,
            "name": redacted.name,
            "size_bytes": redacted.size_bytes,
            "sha256": redacted.sha256,
        },
        "lines": lines,
        "rules": rules,
        "total_hits": sum(counts.values()),
        "rules_disabled": [rule.name for rule in RULES if rule.name not in enabled],
        "created_at": time.time(),
    }


def _store_report(context: JobContext, report: dict[str, Any]) -> Artifact:
    """Write the report as an artifact.

    A thin wrapper on ``store_json`` so the report is stored, hashed and
    deleted by exactly the same path as every other artifact; the name it uses
    is the one ``report_from_job`` looks for.
    """
    return store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=REPORT_ARTIFACT,
        payload=report,
    )


def report_from_job(conn, data_dir, job_id: str) -> dict[str, Any] | None:
    """Read back the report a completed redaction job stored, or None.

    A report written by an older version still loads: the page reads keys it
    knows and ignores the rest, and a rule missing from an old report is shown
    from ``RULES`` with no count rather than raising.
    """
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


def report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The report's per-rule rows, filled out from ``RULES``.

    Reads the stored rows where they exist and falls back to the rule's own
    title and placeholder otherwise, so a report written before a rule was
    added still renders every rule the running version has.
    """
    stored = {
        row.get("name"): row
        for row in report.get("rules", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    rows = []
    for rule in RULES:
        row = stored.get(rule.name, {})
        rows.append(
            {
                "name": rule.name,
                "title": row.get("title") or rule.title,
                "placeholder": row.get("placeholder") or rule.placeholder,
                "enabled": bool(row.get("enabled", True)),
                "hits": int(row.get("hits") or 0),
            }
        )
    # A rule in the report that this version no longer has still gets a row:
    # dropping it would silently lose hits the run actually recorded.
    for name, row in stored.items():
        if rule_by_name(name) is None:
            rows.append(
                {
                    "name": name,
                    "title": row.get("title") or name,
                    "placeholder": row.get("placeholder") or "",
                    "enabled": bool(row.get("enabled", True)),
                    "hits": int(row.get("hits") or 0),
                }
            )
    return rows


register(KIND, run)
