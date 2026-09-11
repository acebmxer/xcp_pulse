"""The "Findings from logs" job: inspect a stored log bundle locally."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app.artifacts import artifact_path, list_for_job, read_json, store_file, store_json
from app.findings import Finding, Report, SourceResult, collect_log_findings, sort_findings
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.redact import enabled_rules

KIND = "log_findings"
LOG_FINDINGS_ARTIFACT = "log-findings.json"
LOG_FINDINGS_MARKDOWN = "log-findings.md"


def run(context: JobContext) -> None:
    job = get_job(context.conn, context.job_id)
    artifact_id = job.params.get("artifact_id") if job else None
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("No log bundle was named.")

    source = next(
        (artifact for artifact in _source_artifacts(context.conn, artifact_id)),
        None,
    )
    if source is None:
        raise ValueError("The selected log bundle is no longer stored.")

    bundle_path = artifact_path(context.data_dir, source.job_id, source.id)
    if not bundle_path.is_file():
        raise ValueError(f"The body of {source.name} is missing from the data volume.")

    context.progress(10, f"Reading {source.name}")
    report = collect_log_findings(
        bundle_path,
        enabled=enabled_rules(context.conn),
        progress=context.progress,
    )
    context.progress(90, "Storing the log findings report")
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=LOG_FINDINGS_ARTIFACT,
        payload=to_payload(report, source.id),
    )
    markdown_path = artifact_path(context.data_dir, context.job_id, "log-findings.md.tmp")
    markdown_path.write_text(to_markdown(report, source.name), encoding="utf-8")
    store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=LOG_FINDINGS_MARKDOWN,
        media_type="text/markdown",
        source=markdown_path,
    )
    summary = f"{len(report.findings)} log finding(s) from {source.name}"
    if report.truncated:
        summary += " — the bundle ends early"
    context.progress(100, summary)


def _source_artifacts(conn, artifact_id: str):
    """Yield the selected artifact when it belongs to a collected log job."""
    from app.artifacts import get_artifact
    from app.job_collect import KIND as COLLECT_KIND
    from app.jobs import get_job

    artifact = get_artifact(conn, artifact_id)
    if artifact is None:
        return []
    job = get_job(conn, artifact.job_id)
    if job is None or job.kind != COLLECT_KIND or not artifact.name.endswith("-logs.tgz"):
        return []
    return [artifact]


def to_payload(report: Report, source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "created_at": report.created_at or time.time(),
        "window_days": report.window_days,
        "counts": report.counts,
        "findings": [asdict(finding) for finding in report.findings],
        "sources": [asdict(source) for source in report.sources],
        "rules_disabled": report.rules_disabled,
        "truncated": report.truncated,
    }


def report_from_job(conn, data_dir, job_id: str) -> Report | None:
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == LOG_FINDINGS_ARTIFACT),
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
            [_build(Finding, item) for item in _records(payload.get("findings"))]
        ),
        sources=[_build(SourceResult, item) for item in _records(payload.get("sources"))],
        window_days=_as_int(payload.get("window_days"), 0),
        created_at=float(payload.get("created_at") or 0.0),
        rules_disabled=[
            item for item in payload.get("rules_disabled") or [] if isinstance(item, str)
        ],
        truncated=bool(payload.get("truncated")),
    )


def to_markdown(report: Report, source_name: str) -> str:
    generated = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(report.created_at or time.time())
    )
    lines = [
        "# XCP Pulse: findings from collected logs",
        "",
        f"Source: {source_name}",
        "",
        f"Generated {generated}.",
        "",
    ]
    if report.truncated:
        lines += [
            "> **The bundle ended early and was not read in full.** Findings below",
            "> come only from the archive members read before the break.",
            "",
        ]
    if report.is_clean:
        lines.append("No findings. Every log source reported nothing.")
    for finding in report.findings:
        lines += [
            f"## {finding.severity.title()}: {finding.title}",
            "",
            f"Evidence: {finding.evidence}",
            "",
            f"Action: {finding.action}",
            "",
        ]
    return "\n".join(lines)


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _build(model, record: dict[str, Any]):
    fields = {field for field in model.__dataclass_fields__}
    return model(**{key: value for key, value in record.items() if key in fields})


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


register(KIND, run)
