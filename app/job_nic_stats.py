"""The "NIC statistics" job: driver error counters over SSH, link state from XO.

A physical NIC's up/down/carrier/speed is already in Xen Orchestra — the same
data its own PIF status column shows — read here through the same REST API
client every other findings source uses. The one thing XO has no route for,
and the collected log bundle does not carry either, is the driver-level
``ethtool -S`` error/drop/CRC counters; that is the only reason this job
connects to a host directly over SSH at all. See
``app/nic_stats_client.py``'s module docstring for the SSH side.

Xen Orchestra's own PIF list is also what tells a physical NIC apart from one
of a host's many per-VM virtual interfaces (``vifN.M``) — both pass the
``/sys/class/net/*/device`` test the host-side dispatcher script uses to find
real hardware, since Xen's backend vif devices carry that symlink too. A vif
is filtered out of both the findings and the stored report rather than
producing a "check the cable" finding that makes no sense for one.

One job, covering every host the operator ticks, because the report is more
useful compared side by side than read one host at a time — the real incident
this feature was built from was exactly that: two hosts logging the same NFS
server's timeouts over the same window, and the NIC counters worth checking
belong to both at once.

A host that cannot be reached over SSH does not fail the run; it is recorded
as unreachable and every other host still gets read, the same "one source
failing never fails the run" rule every other findings source already
follows. The same tolerance applies to the Xen Orchestra side: if its PIF
list cannot be read, every interface just reports as "unknown" link state
rather than the whole run failing — the SSH-read counters are still real and
still worth having even without that cross-reference.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app.artifacts import artifact_path, list_for_job, read_json, store_file, store_json
from app.crypto import DecryptionError
from app.findings import (
    NIC_ERROR_COUNTERS,
    Finding,
    Report,
    SourceResult,
    collect_nic_stat_findings,
    sort_findings,
)
from app.job_inventory import known_inventory
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.nic_stats_client import fetch_stats, parse_ethtool_stats
from app.redact import enabled_rules
from app.ssh_client import SshError, load_private_key
from app.ssh_connection import DecryptionError as SshDecryptionError
from app.ssh_connection import known_host_key, load_credentials, remember_host_key
from app.xo_client import Network, PifStatus, XoError
from app.xo_connection import build_client

KIND = "nic_stats"

NIC_STATS_ARTIFACT = "nic-stats.json"
NIC_STATS_MARKDOWN = "nic-stats.md"


def run(context: JobContext) -> None:
    """Read NIC statistics from every ticked host and store the report.

    Params: ``host_ids`` (a list of ids from the stored inventory). Each
    host's credentials come from its own stored SSH connection — one key per
    host, not one shared across all of them — so a host with no key saved for
    it is recorded as unreachable rather than failing the whole run, the same
    as a host with no address. Which interfaces to read is decided by the
    host itself, at read time (every interface with a real device behind it)
    — nothing to configure here; Xen Orchestra's own PIF list is what later
    narrows that down to physical NICs.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}

    host_ids = params.get("host_ids")
    if not isinstance(host_ids, list) or not host_ids:
        raise ValueError("No host was selected.")

    inventory = known_inventory(context.conn, context.data_dir)
    hosts = [host for host in inventory.hosts if host.id in set(host_ids)]
    if not hosts:
        raise ValueError("None of the selected hosts are in the stored inventory.")

    context.progress(2, "Reading network status from Xen Orchestra")
    # Unlike the API findings job, Xen Orchestra is not this job's reason to
    # exist — the SSH-read counters below are the whole point, and are still
    # real and worth having without either of these. Each is tried on its own
    # rather than sharing one try/except, so one failing (an older instance
    # whose REST API has no /networks route, say) does not also discard a
    # successful read of the other: every interface just reports as "unknown"
    # link state, or the pool network table is simply left out, respectively.
    try:
        xo_client = build_client(context.conn, context.settings.secret_key)
    except (LookupError, DecryptionError, XoError):
        xo_client = None
    try:
        pif_lookup = xo_client.pifs() if xo_client else {}
    except XoError:
        pif_lookup = {}
    try:
        networks = xo_client.networks() if xo_client else []
    except XoError:
        networks = []
    pif_by_host = {host.name: pif_lookup.get(host.id) for host in hosts}
    network_by_id = {network.id: network for network in networks}
    relevant_pool_ids = {host.pool_id for host in hosts if host.pool_id}
    pool_name_by_id = {pool.id: pool.name for pool in inventory.pools}

    host_stats: dict[str, dict[str, dict[str, int]]] = {}
    unreachable: dict[str, str] = {}
    newly_trusted: list[str] = []

    for index, host in enumerate(hosts):
        context.progress(
            5 + int(80 * index / len(hosts)), f"Reading NIC statistics from {host.name}"
        )
        if not host.address:
            unreachable[host.name] = "no address recorded for this host in the inventory"
            continue
        try:
            credentials = load_credentials(context.conn, host.id, context.settings.secret_key)
        except LookupError:
            unreachable[host.name] = "no SSH key configured for this host"
            continue
        except SshDecryptionError:
            unreachable[host.name] = (
                "the stored SSH key cannot be decrypted — the secret key has changed "
                "since it was saved"
            )
            continue
        try:
            # The decrypted key text is not kept past this point; the parsed
            # PKey object is what the connection below actually uses.
            private_key = load_private_key(credentials.private_key, credentials.passphrase)
        except SshError as exc:
            unreachable[host.name] = str(exc)
            continue
        try:
            output, trusted_new_key = _read_host(
                context, host.address, credentials.port, private_key
            )
        except SshError as exc:
            unreachable[host.name] = str(exc)
            continue
        if trusted_new_key:
            newly_trusted.append(host.name)
        host_stats[host.name] = parse_ethtool_stats(output)

    host_stats = _physical_only(host_stats, pif_by_host)

    context.progress(92, "Building the report")
    report = collect_nic_stat_findings(
        host_stats, unreachable=unreachable, enabled=enabled_rules(context.conn)
    )
    report.interfaces = _interface_records(host_stats, pif_by_host, network_by_id)
    report.networks = _network_records(
        [n for n in networks if not relevant_pool_ids or n.pool_id in relevant_pool_ids],
        pool_name_by_id,
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


def _physical_only(
    host_stats: dict[str, dict[str, dict[str, int]]],
    pif_by_host: dict[str, dict[str, PifStatus] | None],
) -> dict[str, dict[str, dict[str, int]]]:
    """Keep only interfaces Xen Orchestra itself reports as a physical NIC.

    A host's virtual interfaces (``vifN.M``, one per VM) pass the same
    ``/sys/class/net/*/device`` test the dispatcher script uses to find real
    hardware, since Xen's backend vif devices carry that symlink too — this is
    what actually tells them apart, using data the host script has no way to
    know about itself. A host with no PIF lookup at all (Xen Orchestra's PIF
    list could not be read, or this host was not in it) keeps every interface
    it read rather than dropping it, since "no cross-reference" is not the
    same as "not physical."
    """
    filtered: dict[str, dict[str, dict[str, int]]] = {}
    for host, interfaces in host_stats.items():
        known = pif_by_host.get(host)
        if known is None:
            filtered[host] = interfaces
            continue
        filtered[host] = {name: counters for name, counters in interfaces.items() if name in known}
    return filtered


def _interface_records(
    host_stats: dict[str, dict[str, dict[str, int]]],
    pif_by_host: dict[str, dict[str, PifStatus] | None],
    network_by_id: dict[str, Network] | None = None,
) -> list[dict[str, Any]]:
    """Every interface actually read, with its link state and error counters.

    Kept even when a run finds nothing to flag — a clean result still read
    real interfaces, and "what did this check" has to be answerable from the
    stored report itself, not only from a finding that never fires.

    ``network`` and ``nbd`` name the network the NIC's untagged traffic
    belongs to and whether NBD is enabled there — None when the PIF or the
    network lookup itself is unavailable, same tolerance as link state above.
    """
    records: list[dict[str, Any]] = []
    for host in sorted(host_stats):
        known = pif_by_host.get(host)
        for interface in sorted(host_stats[host]):
            pif = known.get(interface) if known else None
            network = network_by_id.get(pif.network_id) if pif and network_by_id else None
            counters = host_stats[host][interface]
            records.append(
                {
                    "host": host,
                    "interface": interface,
                    "attached": pif.attached if pif else None,
                    "carrier": pif.carrier if pif else None,
                    "speed": pif.speed if pif else None,
                    "network": network.name if network else None,
                    "nbd": network.nbd if network else None,
                    "counters": {
                        name: counters[name] for name in NIC_ERROR_COUNTERS if name in counters
                    },
                }
            )
    return records


def _network_records(
    networks: list[Network], pool_name_by_id: dict[str, str]
) -> list[dict[str, Any]]:
    """The pool's own network table, the same one XO's Network tab shows.

    Sorted by name for a stable, readable order — nothing about a network's
    importance is captured by the order XO's API happens to return it in.
    """
    return [
        {
            "id": network.id,
            "name": network.name,
            "pool": pool_name_by_id.get(network.pool_id, network.pool_id),
            "vlan": network.vlan,
            "mtu": network.mtu,
            "nbd": network.nbd,
            "locked": network.locked,
            "automatic": network.automatic,
            "pif_count": network.pif_count,
        }
        for network in sorted(networks, key=lambda network: network.name.lower())
    ]


def to_payload(report: Report) -> dict[str, Any]:
    return {
        "created_at": report.created_at or time.time(),
        "counts": report.counts,
        "findings": [asdict(finding) for finding in report.findings],
        "sources": [asdict(source) for source in report.sources],
        "rules_disabled": report.rules_disabled,
        "interfaces": report.interfaces,
        "networks": report.networks,
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
        interfaces=_records(payload.get("interfaces")),
        networks=_records(payload.get("networks")),
    )


def link_summary(record: dict[str, Any]) -> str:
    """One interface's link state as a short phrase, for the page and Markdown.

    ``attached`` is XAPI's own term for administratively up (plugged into the
    host's networking stack); ``carrier`` is the physical signal. A NIC can be
    attached with no carrier — cable out, switch port down — which is worth
    telling apart from either extreme.
    """
    attached = record.get("attached")
    if attached is None:
        return "unknown"
    if not attached:
        return "unplugged"
    if record.get("carrier"):
        speed = record.get("speed")
        return f"up, connected, {speed} Mb/s" if speed else "up, connected"
    return "up, no carrier"


def errors_summary(record: dict[str, Any]) -> str:
    """One interface's checked error counters as a short phrase."""
    hit = {name: value for name, value in record["counters"].items() if value}
    if not hit:
        return "none"
    return ", ".join(f"{name}: {value:,}" for name, value in hit.items())


def nbd_summary(record: dict[str, Any]) -> str:
    """One interface's network and NBD Connection status as a short phrase.

    This is the field a backup job's own "fall back to full" log names when
    it blames NBD — showing it here means an operator can see whether that is
    actually the cause without opening Xen Orchestra's Network tab.
    """
    network = record.get("network")
    if network is None:
        return "unknown"
    return f"{network} (NBD)" if record.get("nbd") else f"{network} (no NBD)"


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
        lines.append("")
    for finding in report.findings:
        lines += [
            f"## {finding.severity.title()}: {finding.title}",
            "",
            f"Evidence: {finding.evidence}",
            "",
            f"Action: {finding.action}",
            "",
        ]
    if report.networks:
        lines += [
            "## Pool networks",
            "",
            "| Network | Pool | VLAN | MTU | NBD Connection | Locked | Automatic | PIFs |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for network in report.networks:
            vlan = network["vlan"] if network["vlan"] not in (None, -1) else "none"
            lines.append(
                f"| {network['name']} | {network['pool']} | {vlan} | {network['mtu']} "
                f"| {'yes' if network['nbd'] else 'no'} "
                f"| {'yes' if network['locked'] else 'no'} "
                f"| {'yes' if network['automatic'] else 'no'} | {network['pif_count']} |"
            )
        lines.append("")
    if report.interfaces:
        lines += [
            "## Interfaces read",
            "",
            "| Host | Interface | Link | Network | Errors |",
            "| --- | --- | --- | --- | --- |",
        ]
        for record in report.interfaces:
            lines.append(
                f"| {record['host']} | {record['interface']} | {link_summary(record)} "
                f"| {nbd_summary(record)} | {errors_summary(record)} |"
            )
        lines.append("")
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
