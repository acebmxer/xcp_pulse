"""The "NIC statistics" job: read ethtool driver counters straight off hosts.

Xen Orchestra has no route for this and the collected log bundle does not
carry it either — see ``app/nic_stats_client.py``'s module docstring for why
this is the one job in the application that connects to a host directly
instead of going through the XO API or a downloaded bundle.

One job, covering every host the operator ticks, because the report is more
useful compared side by side than read one host at a time — the real incident
this feature was built from was exactly that: two hosts logging the same NFS
server's timeouts over the same window, and the NIC counters worth checking
belong to both at once.

A host that cannot be reached does not fail the run; it is recorded as
unreachable and every other host still gets read, the same "one source
failing never fails the run" rule every other findings source already
follows.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app.artifacts import artifact_path, list_for_job, read_json, store_file, store_json
from app.findings import Finding, Report, SourceResult, collect_nic_stat_findings, sort_findings
from app.job_inventory import known_inventory
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.nic_stats_client import fetch_stats, parse_ethtool_stats
from app.redact import enabled_rules
from app.ssh_client import SshError, load_private_key
from app.ssh_connection import known_host_key, load_credentials, remember_host_key

KIND = "nic_stats"

NIC_STATS_ARTIFACT = "nic-stats.json"
NIC_STATS_MARKDOWN = "nic-stats.md"


def run(context: JobContext) -> None:
    """Read NIC statistics from every ticked host and store the report.

    Params: ``host_ids`` (a list of ids from the stored inventory). The
    credentials used come from the stored SSH connection, shared with any
    other host-level check, not from job params — there is exactly one such
    connection, the same way there is exactly one XO connection. Which
    interfaces to read is decided by the host itself, at read time (every
    interface with a real device behind it) — nothing to configure here.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}

    host_ids = params.get("host_ids")
    if not isinstance(host_ids, list) or not host_ids:
        raise ValueError("No host was selected.")

    context.progress(5, "Reading the SSH connection")
    credentials = load_credentials(context.conn, context.settings.secret_key)
    # The decrypted key text is not kept past this point; the parsed PKey
    # object is what every connection below actually uses.
    private_key = load_private_key(credentials.private_key, credentials.passphrase)
    port = credentials.port
    del credentials

    inventory = known_inventory(context.conn, context.data_dir)
    hosts = [host for host in inventory.hosts if host.id in set(host_ids)]
    if not hosts:
        raise ValueError("None of the selected hosts are in the stored inventory.")

    host_stats: dict[str, dict[str, dict[str, int]]] = {}
    unreachable: dict[str, str] = {}
    newly_trusted: list[str] = []

    for index, host in enumerate(hosts):
        context.progress(
            10 + int(80 * index / len(hosts)), f"Reading NIC statistics from {host.name}"
        )
        if not host.address:
            unreachable[host.name] = "no address recorded for this host in the inventory"
            continue
        try:
            output, trusted_new_key = _read_host(context, host.address, port, private_key)
        except SshError as exc:
            unreachable[host.name] = str(exc)
            continue
        if trusted_new_key:
            newly_trusted.append(host.name)
        host_stats[host.name] = parse_ethtool_stats(output)

    context.progress(92, "Building the report")
    report = collect_nic_stat_findings(
        host_stats, unreachable=unreachable, enabled=enabled_rules(context.conn)
    )
    store_json(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=NIC_STATS_ARTIFACT,
        payload=to_payload(report),
    )
    markdown = to_markdown(report, newly_trusted=newly_trusted)
    markdown_path = artifact_path(context.data_dir, context.job_id, "nic-stats.md.tmp")
    markdown_path.write_text(markdown, encoding="utf-8")
    store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=NIC_STATS_MARKDOWN,
        media_type="text/markdown",
        source=markdown_path,
    )

    summary = f"{len(host_stats)} host(s) read, {len(report.findings)} finding(s)"
    if unreachable:
        summary += f", {len(unreachable)} unreachable"
    context.progress(100, summary)


def _read_host(context: JobContext, address: str, port: int, private_key) -> tuple[str, bool]:
    """Fetch one host's raw ethtool output, trusting its SSH key on first use."""
    recorded = known_host_key(context.conn, address)
    trusted: list[bool] = []

    def _on_trust(key_type: str, key_bytes: bytes) -> None:
        remember_host_key(context.conn, address, key_type, key_bytes)
        trusted.append(True)

    output, _ = fetch_stats(
        host=address,
        port=port,
        private_key=private_key,
        known_host_key=recorded,
        on_trust_new_host_key=_on_trust,
    )
    return output, bool(trusted)


def to_payload(report: Report) -> dict[str, Any]:
    return {
        "created_at": report.created_at or time.time(),
        "counts": report.counts,
        "findings": [asdict(finding) for finding in report.findings],
        "sources": [asdict(source) for source in report.sources],
        "rules_disabled": report.rules_disabled,
    }


def report_from_job(conn, data_dir, job_id: str) -> Report | None:
    artifact = next(
        (item for item in list_for_job(conn, job_id) if item.name == NIC_STATS_ARTIFACT),
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
        window_days=0,
        created_at=float(payload.get("created_at") or 0.0),
        rules_disabled=[
            item for item in payload.get("rules_disabled") or [] if isinstance(item, str)
        ],
    )


def to_markdown(report: Report, *, newly_trusted: list[str] | None = None) -> str:
    generated = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(report.created_at or time.time())
    )
    lines = [
        "# XCP Pulse: NIC statistics",
        "",
        f"Generated {generated}.",
        "",
    ]
    if newly_trusted:
        lines += [
            "> **First connection to:** " + ", ".join(newly_trusted) + ". Its SSH host key was "
            "recorded and will be required to match on every future connection.",
            "",
        ]
    if report.is_clean:
        lines.append("No findings. Every host read reported no packet errors.")
    for finding in report.findings:
        lines += [
            f"## {finding.severity.title()}: {finding.title}",
            "",
            f"Evidence: {finding.evidence}",
            "",
            f"Action: {finding.action}",
            "",
        ]
    for source in report.sources:
        if source.detail:
            lines.append(f"_{source.title}: {source.detail}_")
    return "\n".join(lines)


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _build(model, record: dict[str, Any]):
    fields = {field for field in model.__dataclass_fields__}
    return model(**{key: value for key, value in record.items() if key in fields})


register(KIND, run)
