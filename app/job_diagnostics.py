"""The "Collect XO diagnostics" job: raw per-source detail, stored and masked.

Findings (``job_findings``) answers "is anything wrong?" by classifying events
into a short list of problems. This job answers a different question — "what
exactly happened?" — for the sources that classification throws detail away
from: full backup/restore run detail trees, XAPI tasks, and messages/alarms,
archived instance-wide so one download carries everything a support ticket
needs.

It downloads nothing from a host — every source is a Xen Orchestra API call —
but it is not free the way findings is: backup/restore detail is fetched for
*every* run in the window, one request per run, so a busy pool can take a few
seconds rather than one. This is an archive feature, not a failures-only one,
which is why detail is not limited to runs that already failed.

Three sources, each its own pair of artifacts (raw and redacted, mirroring
``job_redact``'s "only the redacted copy ever leaves the box" convention),
plus one report artifact holding what was read and the per-rule hit counts —
the same shape ``job_redact.build_report`` writes, so the existing report
table on the Collect page renders it with no new template code.

Structurally mirrors ``findings.collect_findings``'s "try each source
independently, record read/refused" loop, without importing its
classification pipeline (``Finding``, ``Report``, ``collect_findings``) — this
module's output is raw archived JSON, not Findings. It does reuse
``findings``'s low-level timestamp helpers (``millis_to_seconds``/
``seconds_value``) and ``disabled_rule_titles``, which are public for exactly
this reason.
"""

from __future__ import annotations

import time
from typing import Any

from app.artifacts import Artifact, list_for_job, read_json, store_json
from app.findings import (
    DEFAULT_WINDOW_DAYS,
    backup_failure_message,
    disabled_rule_titles,
    millis_to_seconds,
    seconds_value,
)
from app.job_redact import redacted_name
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.log_dates import range_from_form
from app.redact import RULES, build_username_rule, enabled_rules, redact_json
from app.xo_client import XoClient, XoError
from app.xo_connection import build_client

KIND = "collect_diagnostics"

SOURCE_BACKUP_RESTORE = "backup_restore"
SOURCE_TASKS = "tasks"
SOURCE_MESSAGES_ALARMS = "messages_alarms"
SOURCES = (SOURCE_BACKUP_RESTORE, SOURCE_TASKS, SOURCE_MESSAGES_ALARMS)

SOURCE_TITLES = {
    SOURCE_BACKUP_RESTORE: "Backup and restore runs",
    SOURCE_TASKS: "XAPI tasks",
    SOURCE_MESSAGES_ALARMS: "Messages and alarms",
}

BACKUP_RESTORE_ARTIFACT = "backup-restore-detail.json"
TASKS_ARTIFACT = "xapi-tasks.json"
MESSAGES_ALARMS_ARTIFACT = "messages-alarms.json"
REPORT_ARTIFACT = "diagnostics-report.json"

# The same three statuses ``findings.py``'s ``_failed_runs`` treats as a
# failure. A run in one of these is worth a line in the report's own
# ``failures`` summary — see ``_failure_summaries`` — rather than leaving an
# operator to find it by eye inside a multi-hundred-KiB detail archive.
FAILURE_STATUSES = frozenset({"failure", "error", "interrupted"})

_SOURCE_ARTIFACTS = {
    SOURCE_BACKUP_RESTORE: BACKUP_RESTORE_ARTIFACT,
    SOURCE_TASKS: TASKS_ARTIFACT,
    SOURCE_MESSAGES_ALARMS: MESSAGES_ALARMS_ARTIFACT,
}


def run(context: JobContext) -> None:
    """Read every ticked source, mask it, and store both copies plus a report.

    Params: ``sources`` (a list of ``SOURCES`` keys) and
    ``date_preset``/``date_start``/``date_end`` (the same date-range form
    fields ``job_extract`` and ``job_redact`` already use). No ``pool_id`` and
    no chaining to another job — every source here is instance-wide (see the
    plan's "Decisions already made"), so there is nothing to scope to and
    nothing to wait on.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}

    keys = _valid_sources(params.get("sources"))
    if not keys:
        raise ValueError("No diagnostics source was selected.")

    date_range = range_from_form(
        preset=params.get("date_preset"),
        start_date=params.get("date_start"),
        end_date=params.get("date_end"),
    )

    context.progress(5, "Connecting to Xen Orchestra")
    client = build_client(context.conn, context.settings.secret_key)
    enabled = enabled_rules(context.conn)

    moment = time.time()
    if date_range is not None:
        cutoff = date_range.start if date_range.start is not None else 0.0
        end = date_range.end
    else:
        cutoff = moment - DEFAULT_WINDOW_DAYS * 86400
        end = None

    context.progress(10, "Reading accounts for redaction")
    username_rule = _build_username_rule(client)

    results: list[dict[str, Any]] = []
    hits: dict[str, int] = {}
    failures: list[dict[str, Any]] = []

    if SOURCE_BACKUP_RESTORE in keys:
        context.progress(25, "Reading backup and restore runs")
        _run_backup_restore(
            context, client, cutoff, end, enabled, username_rule, results, hits, failures
        )

    if SOURCE_TASKS in keys:
        context.progress(55, "Reading XAPI tasks")
        _run_tasks(context, client, cutoff, end, enabled, username_rule, results, hits)

    if SOURCE_MESSAGES_ALARMS in keys:
        context.progress(80, "Reading messages and alarms")
        _run_messages_alarms(context, client, cutoff, end, enabled, username_rule, results, hits)

    context.progress(95, "Writing the report")
    report = {
        "created_at": moment,
        "date_start": date_range.start if date_range is not None else None,
        "date_end": date_range.end if date_range is not None else None,
        "sources_requested": list(keys),
        "sources": results,
        "failures": failures,
        "rules_disabled": disabled_rule_titles(enabled),
        "rules": _rule_rows(enabled, hits),
    }
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=REPORT_ARTIFACT,
        payload=report,
    )

    refused = [item["title"] for item in results if not item["read"]]
    summary = f"{len(keys)} source(s) requested, {sum(hits.values())} value(s) masked"
    if failures:
        summary += f", {len(failures)} failed run(s)"
    if refused:
        summary += f"; refused: {', '.join(refused)}"
    context.progress(100, summary)


def _run_backup_restore(
    context: JobContext,
    client: XoClient,
    cutoff: float,
    end: float | None,
    enabled,
    username_rule,
    results: list[dict[str, Any]],
    hits: dict[str, int],
    failures: list[dict[str, Any]],
) -> None:
    """Full detail for every backup and restore run in the window.

    Both routes are read inside one try block because the checkbox that
    triggers this covers both — a caller ticks "backup and restore runs" as
    one source, not two. Detail is fetched for every enumerated run, not just
    failed ones: this is an archive feature, and a run whose own detail fetch
    fails still keeps its summary, with ``detail_error`` in place of
    ``detail``, rather than losing the rest of the batch.
    """
    try:
        backups = [record for record in client.backup_logs(cutoff) if _within(record, end)]
        restores = [record for record in client.restore_logs(cutoff) if _within(record, end)]
    except XoError as exc:
        results.append(_source_result(SOURCE_BACKUP_RESTORE, read=False, reason=str(exc)))
        return

    detail_count = 0
    for record in backups:
        detail_count += _attach_detail(record, client.backup_log_detail)
    for record in restores:
        detail_count += _attach_detail(record, client.restore_log_detail)

    payload = {"backups": backups, "restores": restores}
    masked = _store_source(context, enabled, username_rule, hits, BACKUP_RESTORE_ARTIFACT, payload)
    failures.extend(_failure_summaries(masked["backups"]) + _failure_summaries(masked["restores"]))
    results.append(
        _source_result(
            SOURCE_BACKUP_RESTORE,
            read=True,
            count=len(backups) + len(restores),
            detail_count=detail_count,
        )
    )


def _run_tasks(
    context: JobContext,
    client: XoClient,
    cutoff: float,
    end: float | None,
    enabled,
    username_rule,
    results: list[dict[str, Any]],
    hits: dict[str, int],
) -> None:
    try:
        tasks = [record for record in client.tasks(cutoff) if _within(record, end)]
    except XoError as exc:
        results.append(_source_result(SOURCE_TASKS, read=False, reason=str(exc)))
        return

    _store_source(context, enabled, username_rule, hits, TASKS_ARTIFACT, {"tasks": tasks})
    results.append(_source_result(SOURCE_TASKS, read=True, count=len(tasks)))


def _run_messages_alarms(
    context: JobContext,
    client: XoClient,
    cutoff: float,
    end: float | None,
    enabled,
    username_rule,
    results: list[dict[str, Any]],
    hits: dict[str, int],
) -> None:
    try:
        messages = [
            record for record in client.messages(cutoff) if _within(record, end, millis=False)
        ]
        alarms = [record for record in client.alarms(cutoff) if _within(record, end, millis=False)]
    except XoError as exc:
        results.append(_source_result(SOURCE_MESSAGES_ALARMS, read=False, reason=str(exc)))
        return

    payload = {"messages": messages, "alarms": alarms}
    _store_source(context, enabled, username_rule, hits, MESSAGES_ALARMS_ARTIFACT, payload)
    results.append(
        _source_result(SOURCE_MESSAGES_ALARMS, read=True, count=len(messages) + len(alarms))
    )


def _attach_detail(record: dict[str, Any], detail_fn) -> int:
    """Fetch one run's full detail tree onto its summary record. Returns 0 or 1.

    A per-run failure sets ``detail_error`` rather than raising, so one
    unreachable run's detail does not lose the rest of the batch.
    """
    log_id = record.get("id")
    if not log_id:
        return 0
    try:
        record["detail"] = detail_fn(str(log_id))
        return 1
    except XoError as exc:
        record["detail_error"] = str(exc)
        return 0


def _store_source(
    context: JobContext,
    enabled,
    username_rule,
    hits: dict[str, int],
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Store a source's raw payload, then a masked copy under ``redacted_name``.

    Returns the masked payload, so a caller that needs to read something back
    out of it — ``_run_backup_restore``'s failure headlines — reads the same
    already-redacted copy this stores, rather than re-deriving it or reading
    the unmasked one.

    ``redact_json`` walks the parsed value rather than a serialized string, so
    a hit landing on a quote character can never corrupt the document — see
    that function's own docstring.
    """
    store_json(context.conn, context.data_dir, job_id=context.job_id, name=name, payload=payload)
    masked, counts = redact_json(payload, enabled, username_rule=username_rule)
    for rule_name, count in counts.items():
        hits[rule_name] = hits.get(rule_name, 0) + count
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=redacted_name(name),
        payload=masked,
    )
    return masked


def _failure_summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per run whose status already shows a failure, headline included.

    Built from the *masked* records ``_store_source`` already produced —
    never the raw ones — so a headline surfaced on the page or in the report
    is never less redacted than the archive artifact it was read from. The
    headline itself is ``findings.backup_failure_message`` — the same read
    Findings' own "Backup job failed" finding now uses, so the two features
    can never disagree about what a run's detail tree says went wrong.
    """
    summaries = []
    for record in records:
        status = str(record.get("status") or "").lower()
        if status not in FAILURE_STATUSES:
            continue
        summaries.append(
            {
                "job_name": str(record.get("jobName") or record.get("jobId") or "unnamed job"),
                "status": status,
                "headline": backup_failure_message(record.get("detail")),
            }
        )
    return summaries


def _source_result(
    name: str, *, read: bool, reason: str = "", count: int = 0, detail_count: int = 0
) -> dict[str, Any]:
    return {
        "name": name,
        "title": SOURCE_TITLES.get(name, name),
        "read": read,
        "reason": reason,
        "count": count,
        "detail_count": detail_count,
    }


def _rule_rows(enabled, hits: dict[str, int]) -> list[dict[str, Any]]:
    """Every rule's hit count, in the same shape ``job_redact.build_report``
    writes — so ``job_redact.report_rows`` renders this report with no new
    template code, on the same table every other job's report already uses.
    """
    return [
        {
            "name": rule.name,
            "title": rule.title,
            "placeholder": rule.placeholder,
            "enabled": rule.name in enabled,
            "hits": hits.get(rule.name, 0),
        }
        for rule in RULES
    ]


def _build_username_rule(client: XoClient):
    """The username redaction rule, built from a live account list.

    A ``users()`` failure — a restricted account, or XO briefly unreachable —
    is not fatal to the job. It only means usernames go unmasked by name, the
    same degraded-but-usable result a disabled redaction rule already gives.
    """
    try:
        accounts = client.users()
    except XoError:
        accounts = []
    return build_username_rule(str(account.get("email") or "") for account in accounts)


def _within(record: dict[str, Any], end: float | None, *, millis: bool = True) -> bool:
    """Whether a record's own end (or start) falls at or before ``end``.

    Xen Orchestra's routes take only a lower bound (already applied as the
    ``since`` filter each list call sends), so an upper bound — a date range's
    end — is enforced here against records the server already returned, the
    same way ``findings.collect_findings`` does it. Backup/restore runs and
    tasks carry milliseconds; messages and alarms carry seconds — see
    ``findings.millis_to_seconds`` for why mixing the two is a silent bug.
    """
    if end is None:
        return True
    if millis:
        at = millis_to_seconds(record.get("end")) or millis_to_seconds(record.get("start"))
    else:
        at = seconds_value(record.get("time"))
    if at is None:
        return True
    return at <= end


def _valid_sources(raw: object) -> list[str]:
    """The submitted source keys that are actually real ones, in display order."""
    if not isinstance(raw, list):
        return []
    valid = set(SOURCES)
    seen = {key for key in raw if isinstance(key, str) and key in valid}
    return [key for key in SOURCES if key in seen]


def diagnostics_artifacts_from_job(conn, job_id: str) -> list[Artifact]:
    """The redacted copies this job produced — never the raw ones.

    Same "only the redacted copy ever leaves the box" rule
    ``job_support_package`` already applies to the collected log bundle, for
    the same future use: anything that reuses a diagnostics run's output
    (a support package, later) must never be able to reach the raw copies.
    """
    return [item for item in list_for_job(conn, job_id) if ".redacted." in item.name]


def report_from_job(conn, data_dir, job_id: str) -> dict[str, Any] | None:
    """Read back a stored diagnostics report, or None.

    A plain dict rather than a dataclass — the page only reads it back, so a
    key an older stored report lacks is simply absent rather than a reason to
    reject the whole artifact. ``report["failures"]`` is one entry per backup
    or restore run whose status already shows a failure, each with the
    headline ``_run_headline`` pulled out of its detail tree where that shape
    was there — a report written before this field existed simply has none.
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


register(KIND, run)
