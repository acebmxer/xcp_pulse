"""The "Findings from the API" job: ask Xen Orchestra what is wrong, store it.

Collection downloads 433 MB and takes two minutes. This asks seven routes and
takes a second, needs no ``export:logs`` privilege, and answers the question an
operator actually opens the application with — *is anything wrong?* — before
anything has been downloaded at all.

Two artifacts come out of it, because a findings report has two readers:

* **JSON**, which is what this module reads back to render the page, and what
  the support package will carry.
* **Markdown**, which is what goes into a support ticket or an email. Generated
  here rather than in a template, so the file on disk and the page are built
  from the same report.

The stored report is a *result*, in the same store as every other artifact, so
it is listed, hashed, downloaded and deleted by code that already exists.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app.artifacts import artifacts_dir, list_for_job, read_json, store_file, store_json
from app.findings import (
    SEVERITIES,
    Finding,
    Report,
    SourceResult,
    collect_findings,
    sort_findings,
)
from app.job_inventory import known_inventory
from app.job_runner import register
from app.jobs import JobContext
from app.redact import enabled_rules
from app.xo_connection import build_client

KIND = "api_findings"

# The artifacts this job writes. One name each, used to write them and to find
# them again, so a reader cannot drift from its writer.
FINDINGS_ARTIFACT = "findings.json"
FINDINGS_MARKDOWN = "findings.md"


def run(context: JobContext) -> None:
    """Read every findings source and store the report.

    The pool list comes from the stored inventory rather than a fresh call, for
    the same reason the collect page takes its hosts from there: the
    missing-patches route needs a pool id, and the inventory is what the
    operator has already seen. A run with no stored inventory still works —
    every other source is pool-independent — and records the patch source as
    unread with that as the reason.
    """
    context.progress(5, "Connecting to Xen Orchestra")
    client = build_client(context.conn, context.settings.secret_key)

    # The rules switched on now, so evidence in a findings report is masked the
    # same way a collected bundle is. A report is a thing people send onward.
    enabled = enabled_rules(context.conn)
    inventory = known_inventory(context.conn, context.data_dir)

    report = collect_findings(
        client,
        inventory.pools,
        enabled=enabled,
        progress=context.progress,
    )

    context.progress(95, "Storing the report")
    payload = to_payload(report)
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=FINDINGS_ARTIFACT,
        payload=payload,
    )
    _store_markdown(context, report)

    counts = report.counts
    context.progress(
        100,
        f"{len(report.findings)} finding(s): "
        f"{counts['critical']} critical, {counts['warning']} warning, {counts['info']} info",
    )


def to_payload(report: Report) -> dict[str, Any]:
    """The report as plain JSON.

    ``asdict`` rather than a hand-written mapping, so a field added to Finding
    is stored without this being edited — and ``report_from_job`` below fills
    anything missing from an older artifact with the dataclass default.
    """
    return {
        "created_at": report.created_at or time.time(),
        "window_days": report.window_days,
        "counts": report.counts,
        "findings": [asdict(finding) for finding in report.findings],
        "sources": [asdict(source) for source in report.sources],
        "rules_disabled": report.rules_disabled,
    }


def report_from_job(conn, data_dir, job_id: str) -> Report | None:
    """Rebuild the report a completed findings job stored, or None.

    Unknown keys are dropped and missing ones left at their default, so a
    report written by an older version still renders rather than raising on a
    field that has since changed — the same tolerance ``inventory_from_job``
    has, for the same reason.
    """
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == FINDINGS_ARTIFACT),
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

    return Report(
        findings=sort_findings(
            [_build(Finding, record) for record in _records(payload.get("findings"))]
        ),
        sources=[_build(SourceResult, record) for record in _records(payload.get("sources"))],
        window_days=_as_int(payload.get("window_days"), Report.window_days),
        created_at=float(payload.get("created_at") or 0.0),
        rules_disabled=[
            str(title) for title in payload.get("rules_disabled") or [] if isinstance(title, str)
        ],
    )


def to_markdown(report: Report) -> str:
    """The report as Markdown, for a support ticket.

    Grouped by severity rather than by source: someone reading this wants the
    worst thing first, and which API route it came from is a detail on the
    finding, not a way to organise the document.
    """
    created = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(report.created_at or time.time()))
    counts = report.counts

    lines = [
        "# XCP Pulse: findings from the Xen Orchestra API",
        "",
        f"Generated {created}, covering the last {report.window_days} days.",
        "",
        f"**{counts['critical']} critical, {counts['warning']} warning, "
        f"{counts['info']} informational.**",
        "",
    ]

    if report.rules_disabled:
        # Before the findings, not after: someone about to send this needs to
        # know the evidence below is only partly masked while they can still
        # decide not to send it.
        lines += [
            f"> **{len(report.rules_disabled)} redaction rule(s) were switched off:** "
            f"{', '.join(report.rules_disabled)}. Evidence below is masked only by "
            f"the rules that were on.",
            "",
        ]

    if report.is_clean:
        lines += ["No findings. Every source that could be read reported nothing.", ""]

    for severity in SEVERITIES:
        group = [finding for finding in report.findings if finding.severity == severity]
        if not group:
            continue
        lines += [f"## {severity.title()}", ""]
        for finding in group:
            lines += _markdown_finding(finding)

    lines += [
        "## Sources",
        "",
        "Xen Orchestra serves all of these, but only some originate there; the",
        "rest it relays from the XCP-ng hosts.",
        "",
        "| Source | Comes from | What it holds | Result |",
        "| --- | --- | --- | --- |",
    ]
    for source in report.sources:
        if source.read:
            result = source.examined_text
        else:
            result = f"**not read**: {source.reason}"
        result = result.replace("|", "\\|")
        lines.append(
            f"| {source.title} | {source.origin or '-'} | {source.info.holds} | {result} |"
        )
    lines.append("")

    return "\n".join(lines)


def _markdown_finding(finding: Finding) -> list[str]:
    """One finding as a Markdown block."""
    heading = f"### {finding.title}"
    if finding.count > 1:
        heading += f" ({finding.count} times)"

    block = [heading, ""]
    if finding.at:
        seen = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(finding.at))
        block.append(f"*Most recent: {seen}. Source: {finding.source_title}.*")
    else:
        block.append(f"*Source: {finding.source_title}.*")
    block += ["", "```", finding.evidence, "```", "", f"**What to do:** {finding.action}", ""]
    return block


def _store_markdown(context: JobContext, report: Report) -> None:
    """Write the Markdown copy as an artifact.

    ``store_json`` would wrap the text in JSON quotes, so this is the one place
    a text artifact is written: to a temporary file under the data directory,
    then handed to ``store_file``, which moves it in and hashes it exactly as
    it does a 433 MB bundle. No second store, and no second delete path.
    """
    staging = artifacts_dir(context.data_dir) / f"{context.job_id}.md.tmp"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(to_markdown(report), encoding="utf-8")
    try:
        store_file(
            context.conn,
            context.data_dir,
            job_id=context.job_id,
            name=FINDINGS_MARKDOWN,
            source=staging,
            media_type="text/markdown",
        )
    finally:
        staging.unlink(missing_ok=True)


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _build(cls, record: dict[str, Any]):
    fields = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{key: value for key, value in record.items() if key in fields})


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


register(KIND, run)
