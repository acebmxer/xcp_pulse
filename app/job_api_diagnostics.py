"""The "Collect XO diagnostics" job: raw per-run detail, stored and masked.

Findings (``job_findings``) answers "is anything wrong?" by classifying
events into a short list of problems. This job answers a different question —
"what exactly happened?" — for the three sources that classification throws
detail away from: backup and restore runs (a failure finding says a job
failed; this stores the full per-VM/per-disk task tree behind it) and, along
with them, the same window's XAPI tasks, messages and alarms, so one download
carries everything a support ticket needs instead of five.

It downloads nothing from a host — every source is a Xen Orchestra API call —
but it is not free the way findings is: a backup or restore run's detail is
its own request, one per failed run in the window, so a busy pool can make
this take a few seconds rather than one.

Two artifacts come out of it, the same split as findings: JSON for the page,
Markdown for a support ticket. Both are masked with the redaction rules
switched on when the job ran, including usernames — the one rule with no
fixed pattern of its own, built here from a live account list the same way
``job_findings`` will once it grows the same need.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.artifacts import artifacts_dir, list_for_job, read_json, store_file, store_json
from app.findings import DEFAULT_WINDOW_DAYS, disabled_rule_titles, millis_to_seconds, seconds_value
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.log_dates import range_from_form
from app.redact import build_username_rule, enabled_rules, redact_json
from app.xo_client import XoClient, XoError
from app.xo_connection import build_client

KIND = "api_diagnostics"

DIAGNOSTICS_ARTIFACT = "diagnostics.json"
DIAGNOSTICS_MARKDOWN = "diagnostics.md"

# The same three statuses ``findings.py``'s ``_failed_runs`` treats as a
# failure, so a run this job fetches full detail for is exactly the run
# findings would have reported.
FAILURE_STATUSES = frozenset({"failure", "error", "interrupted"})

RUN_CATEGORIES = ("backups", "restores")
EVENT_CATEGORIES = ("tasks", "messages", "alarms")
CATEGORY_TITLES = {
    "backups": "Backup runs",
    "restores": "Restore runs",
    "tasks": "XAPI tasks",
    "messages": "XAPI messages",
    "alarms": "Alarms",
}


@dataclass(frozen=True)
class CategoryResult:
    """Whether one source was read, and how much of it."""

    name: str
    read: bool
    reason: str = ""
    count: int = 0
    detail_count: int = 0

    @property
    def title(self) -> str:
        return CATEGORY_TITLES.get(self.name, self.name)


def _category_payload(item: CategoryResult) -> dict[str, Any]:
    """A category as a plain dict, ``title`` included.

    Built by hand rather than ``asdict`` — ``title`` is a property, not a
    dataclass field, and both the page and the Markdown copy need it stored
    rather than recomputed, so a report written today still shows the right
    title even if a category is ever renamed.
    """
    return {
        "name": item.name,
        "title": item.title,
        "read": item.read,
        "reason": item.reason,
        "count": item.count,
        "detail_count": item.detail_count,
    }


def run(context: JobContext) -> None:
    """Read every raw source, fetch detail for failed runs, mask, store."""
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}
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

    categories: list[CategoryResult] = []

    context.progress(20, "Reading backup runs")
    backups = _collect_runs(
        client.backup_logs, client.backup_log_detail, cutoff, end, categories, "backups"
    )

    context.progress(40, "Reading restore runs")
    restores = _collect_runs(
        client.restore_logs, client.restore_log_detail, cutoff, end, categories, "restores"
    )

    context.progress(60, "Reading XAPI tasks")
    tasks = _collect_events(client.tasks, cutoff, end, categories, "tasks", millis=True)

    context.progress(75, "Reading XAPI messages")
    messages = _collect_events(client.messages, cutoff, end, categories, "messages", millis=False)

    context.progress(85, "Reading alarms")
    alarms = _collect_events(client.alarms, cutoff, end, categories, "alarms", millis=False)

    payload: dict[str, Any] = {
        "created_at": moment,
        "date_start": date_range.start if date_range is not None else None,
        "date_end": date_range.end if date_range is not None else None,
        "rules_disabled": disabled_rule_titles(enabled),
        "categories": [_category_payload(item) for item in categories],
        "backups": backups,
        "restores": restores,
        "tasks": tasks,
        "messages": messages,
        "alarms": alarms,
    }

    context.progress(95, "Masking sensitive values")
    masked, hit_counts = redact_json(payload, enabled, username_rule=username_rule)

    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=DIAGNOSTICS_ARTIFACT,
        payload=masked,
    )
    _store_markdown(context, masked)

    context.progress(
        100,
        f"{len(backups)} backup run(s), {len(restores)} restore run(s), "
        f"{len(tasks)} task(s) — {sum(hit_counts.values())} value(s) masked",
    )


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


def _collect_runs(
    list_fn: Callable[[float], list[dict[str, Any]]],
    detail_fn: Callable[[str], dict[str, Any]],
    cutoff: float,
    end: float | None,
    categories: list[CategoryResult],
    name: str,
) -> list[dict[str, Any]]:
    """One run-shaped source (backups or restores), with detail for failures.

    Detail is fetched only for a run whose summary already shows a failure —
    the case a support ticket exists to explain. Fetching it for every run in
    the window would make this job as slow as a full log collection for no
    benefit on the runs that already succeeded.
    """
    try:
        records = list_fn(cutoff)
    except XoError as exc:
        categories.append(CategoryResult(name=name, read=False, reason=str(exc)))
        return []

    runs = [record for record in records if _within(record, end, millis=True)]
    detail_count = 0
    for record in runs:
        if str(record.get("status") or "").lower() not in FAILURE_STATUSES:
            continue
        log_id = record.get("id")
        if not log_id:
            continue
        try:
            record["detail"] = detail_fn(str(log_id))
            detail_count += 1
        except XoError as exc:
            record["detail_error"] = str(exc)

    categories.append(
        CategoryResult(name=name, read=True, count=len(runs), detail_count=detail_count)
    )
    return runs


def _collect_events(
    list_fn: Callable[[float], list[dict[str, Any]]],
    cutoff: float,
    end: float | None,
    categories: list[CategoryResult],
    name: str,
    *,
    millis: bool,
) -> list[dict[str, Any]]:
    """A plain event source (tasks, messages, alarms) — no per-record detail."""
    try:
        records = list_fn(cutoff)
    except XoError as exc:
        categories.append(CategoryResult(name=name, read=False, reason=str(exc)))
        return []

    events = [record for record in records if _within(record, end, millis=millis)]
    categories.append(CategoryResult(name=name, read=True, count=len(events)))
    return events


def _within(record: dict[str, Any], end: float | None, *, millis: bool) -> bool:
    """Whether a record's own end (or start) falls at or before ``end``.

    Xen Orchestra's routes take only a lower bound (already applied as the
    ``since`` filter each ``list_fn`` sends), so an upper bound — a date
    range's end — is enforced here against records the server already
    returned, the same way ``findings.collect_findings`` does it.

    The unit conversion is ``findings.millis_to_seconds``/``seconds_value`` —
    the one place XO's mixed units (task-shaped records carry milliseconds,
    message-shaped ones carry seconds) are converted, so a second, drifting
    copy of that conversion is never written here.
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


def report_from_job(conn, data_dir, job_id: str) -> dict[str, Any] | None:
    """Rebuild the stored diagnostics payload for one job, or None.

    Returned as a plain dict rather than a dataclass — the page only reads it
    back, never rebuilds findings-style objects from it, and an unrecognised
    or missing key here is simply absent from the rendered page rather than a
    reason to reject an artifact written by an older version.
    """
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == DIAGNOSTICS_ARTIFACT),
        None,
    )
    if artifact is None:
        return None
    try:
        payload = read_json(data_dir, artifact)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    payload.setdefault("categories", [])
    payload.setdefault("rules_disabled", [])
    for name in RUN_CATEGORIES + EVENT_CATEGORIES:
        payload.setdefault(name, [])
    return payload


def run_headline(detail: object) -> str | None:
    """The one line of a backup/restore run's detail worth reading first.

    ``detail`` is XO's own nested task tree, shaped however that particular
    job type happened to fail — there is no schema to rely on. The one field
    observed to consistently carry the actual failure text (rather than the
    job type, a task name, or a schedule id) is ``result.message`` — measured
    against a real failed backup run, where the top-level ``message`` was
    just ``"backup"`` and the real reason ("couldn't instantiate any remote")
    was nested under ``result``.

    Returns None rather than guessing when that shape is not there, so a run
    whose detail looks different is shown with no headline rather than a
    wrong one.
    """
    if not isinstance(detail, dict):
        return None
    result = detail.get("result")
    if not isinstance(result, dict):
        return None
    message = result.get("message")
    return message if isinstance(message, str) and message else None


def pretty_detail(detail: object) -> str:
    """A run's detail tree as well-formed, indented JSON.

    Rendering a Python dict with ``str()`` produces its ``repr`` — single
    quotes, no indentation — which is what this replaces. The JSON shape
    itself is left exactly as XO returns it, including a multi-line value
    like a stack trace staying one JSON string with a literal ``\\n`` in it:
    that is correct JSON. ``json.dumps`` with ``ensure_ascii=True`` (the
    default) keeps this ASCII, the same rule the rest of the Markdown
    document holds to.
    """
    return json.dumps(detail, indent=2)


def to_markdown(payload: dict[str, Any]) -> str:
    """The diagnostics payload as Markdown, for a support ticket.

    Pure ASCII, the same rule ``job_findings.to_markdown`` follows: this file
    is emailed and pasted into ticketing systems whose own handling of an
    encoding they were told about cannot be relied on.
    """
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(payload.get("created_at") or time.time())
    )
    categories = payload.get("categories") or []
    rules_disabled = payload.get("rules_disabled") or []

    lines = [
        "# XCP Pulse: raw diagnostics from the Xen Orchestra API",
        "",
        f"Generated {created}.",
        "",
    ]

    if rules_disabled:
        lines += [
            f"> **{len(rules_disabled)} redaction rule(s) were switched off:** "
            f"{', '.join(rules_disabled)}. Values below are masked only by the "
            f"rules that were on.",
            "",
        ]

    lines += ["## Sources", "", "| Source | Result |", "| --- | --- |"]
    for category in categories:
        title = str(category.get("title") or category.get("name"))
        if category.get("read"):
            text = f"{category.get('count', 0)} record(s)"
            if category.get("detail_count"):
                text += f", {category.get('detail_count')} with full detail fetched"
        else:
            text = f"**not read**: {category.get('reason', '')}"
        text = text.replace("|", "\\|")
        lines.append(f"| {title} | {text} |")
    lines.append("")

    failed = [
        record
        for record in (payload.get("backups") or []) + (payload.get("restores") or [])
        if str(record.get("status") or "").lower() in FAILURE_STATUSES
    ]
    if failed:
        lines += ["## Failed backup and restore runs", ""]
        for record in failed:
            name = str(record.get("jobName") or record.get("jobId") or "unnamed job")
            status = str(record.get("status") or "")
            lines.append(f"### {name} ({status})")
            lines.append("")
            if record.get("detail") is not None:
                headline = run_headline(record["detail"])
                if headline:
                    lines += [f"**{headline}**", ""]
                lines += ["```", pretty_detail(record["detail"]), "```", ""]
            elif record.get("detail_error"):
                lines.append(f"*Detail could not be read: {record['detail_error']}*")
                lines.append("")

    return "\n".join(lines)


def _store_markdown(context: JobContext, payload: dict[str, Any]) -> None:
    """Write the Markdown copy as an artifact, the same way findings does."""
    staging = artifacts_dir(context.data_dir) / f"{context.job_id}.md.tmp"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(to_markdown(payload), encoding="utf-8")
    try:
        store_file(
            context.conn,
            context.data_dir,
            job_id=context.job_id,
            name=DIAGNOSTICS_MARKDOWN,
            source=staging,
            media_type="text/markdown",
        )
    finally:
        staging.unlink(missing_ok=True)


register(KIND, run)
