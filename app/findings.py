"""What the Xen Orchestra API says is wrong, turned into findings.

A finding is one problem, stated once, with the evidence that established it.
That shape is the whole module: severity to sort by, a title to read, evidence
to check, an action to take, and the source it came from so the operator can go
and look at the same thing.

Six sources, all read through ``XoClient``:

* **XAPI messages** — the pool's own event record. Mostly routine VM lifecycle
  noise; the interesting rows are storage, HA and master transitions.
* **Alarms** — XO's filtered view of the same event stream.
* **Tasks** — what XO tried to do and failed at, with the stack trace.
* **Missing patches** — per pool, and refused outright on XOA without a
  subscription.
* **Backup and restore runs** — a job that failed, or has not run recently.
* **The pool dashboard** — totals XO has already computed: patch state, backup
  job health, storage headroom, host and VM state.

**Every source is optional.** A findings run reads what the account can reach
and records what it could not, rather than failing because one route was
refused — a restricted account can read messages and tasks and is refused the
dashboard, and a report covering four sources is worth far more than an error.

**Evidence is redacted before it is stored.** Task properties carry usernames
and client addresses, and message bodies carry hostnames and UUIDs. Findings
are a thing an operator attaches to a support ticket, so evidence goes through
the same ``redact_line`` the bundle repack uses, under the same rule settings —
one masking implementation, not a second one for short strings.
"""

from __future__ import annotations

import gzip
import re
import tarfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from app.redact import RULES, redact_line
from app.xo_client import Pool, XoClient, XoError

# Severities, worst first. The order here is the order findings sort in and the
# order they read on the page, so it is defined once and indexed rather than
# being a set of strings compared by hand.
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

SEVERITIES = (CRITICAL, WARNING, INFO)

# The sources a report can carry. A source that produced nothing and a source
# that could not be read are different answers, and both are recorded — the
# same distinction the redaction report draws between a rule with no hits and a
# rule switched off.
SOURCE_MESSAGES = "messages"
SOURCE_ALARMS = "alarms"
SOURCE_TASKS = "tasks"
SOURCE_PATCHES = "patches"
SOURCE_BACKUPS = "backups"
SOURCE_RESTORES = "restores"
SOURCE_DASHBOARD = "dashboard"
SOURCE_LOGS_STORAGE = "logs_storage"
SOURCE_LOGS_MULTIPATH = "logs_multipath"
SOURCE_LOGS_XAPI = "logs_xapi"
SOURCE_LOGS_HA = "logs_ha"

# Where a source's data physically comes from. Xen Orchestra serves all seven
# routes, but it *originates* only three of them — the rest it relays from the
# hosts. That distinction decides where an operator goes to act on a finding:
# a XAPI message means log in to the host, a failed task means look in XO.
ORIGIN_HOSTS = "XCP-ng hosts"
ORIGIN_XO = "Xen Orchestra"


@dataclass(frozen=True)
class SourceInfo:
    """What one source is, in the terms an operator thinks in.

    Held beside the finding rules rather than in the template because the same
    description has to appear on the page and in the Markdown a support ticket
    gets, and two copies of it would drift.
    """

    title: str
    origin: str
    holds: str
    unit: str

    def examined_text(self, count: int, read: bool) -> str:
        """How to phrase this source's record count.

        A bare ``0`` is ambiguous in a way that matters: for alarms it means
        none exist, for patches it means the hosts were checked and are up to
        date. Those are different facts and an operator reading a support
        report has to be able to tell them apart.
        """
        if not read:
            return "-"
        if count:
            return f"{count:,} {self.unit}"
        # "What it holds" is already its own column beside this one, so
        # repeating it here reads as a stutter. The unit is what makes the
        # zero specific: "no alarms" says more than "0".
        return f"checked - no {self.unit}"


SOURCES = {
    SOURCE_MESSAGES: SourceInfo(
        title="XAPI messages",
        origin=ORIGIN_HOSTS,
        holds="the hosts' own event record",
        unit="messages",
    ),
    SOURCE_ALARMS: SourceInfo(
        title="Alarms",
        origin=ORIGIN_HOSTS,
        holds="active host and VM alarms",
        unit="alarms",
    ),
    SOURCE_TASKS: SourceInfo(
        title="Tasks",
        origin=ORIGIN_XO,
        holds="Xen Orchestra's own operations",
        unit="tasks",
    ),
    SOURCE_PATCHES: SourceInfo(
        title="Missing patches",
        origin=ORIGIN_HOSTS,
        holds="each host's available XCP-ng updates",
        unit="patches",
    ),
    SOURCE_BACKUPS: SourceInfo(
        title="Backup runs",
        origin=ORIGIN_XO,
        holds="Xen Orchestra's backup job history",
        unit="runs",
    ),
    SOURCE_RESTORES: SourceInfo(
        title="Restore runs",
        origin=ORIGIN_XO,
        holds="Xen Orchestra's restore history",
        unit="runs",
    ),
    SOURCE_DASHBOARD: SourceInfo(
        title="Pool dashboard",
        origin=ORIGIN_HOSTS,
        holds="pool totals XO computes from the hosts",
        unit="sections",
    ),
    SOURCE_LOGS_STORAGE: SourceInfo(
        title="Storage logs",
        origin=ORIGIN_HOSTS,
        holds="storage and filesystem errors in the collected bundle",
        unit="log lines",
    ),
    SOURCE_LOGS_MULTIPATH: SourceInfo(
        title="Multipath logs",
        origin=ORIGIN_HOSTS,
        holds="multipath path changes and failures",
        unit="log lines",
    ),
    SOURCE_LOGS_XAPI: SourceInfo(
        title="XAPI logs",
        origin=ORIGIN_HOSTS,
        holds="XAPI exceptions and backtraces",
        unit="log lines",
    ),
    SOURCE_LOGS_HA: SourceInfo(
        title="HA logs",
        origin=ORIGIN_HOSTS,
        holds="HA heartbeat and fencing events",
        unit="log lines",
    ),
}

# Kept so a source name this version does not know still renders as itself
# rather than raising — a report written by a later version has to load.
SOURCE_TITLES = {name: info.title for name, info in SOURCES.items()}

# How far back an event still counts as a finding. A pool that lost redundancy
# a year ago and recovered should not be reported as currently degraded, and
# XO keeps messages indefinitely — measured, 3,472 rows going back months.
#
# Findings that are states rather than events — missing patches, a disabled
# host, a backup job with no recent run — come from the dashboard and carry no
# age, so this bounds the event sources only.
DEFAULT_WINDOW_DAYS = 30

# A backup job that has not run in this long is reported even when its last run
# succeeded. A job that silently stopped firing looks healthy by every other
# measure, which is exactly what makes it worth saying.
BACKUP_STALE_DAYS = 7

# Storage past this fraction is reported. Chosen because a thin-provisioned SR
# that fills stops VMs rather than slowing them, so the useful warning is the
# one that arrives with room to act on it.
STORAGE_WARN_FRACTION = 0.85
STORAGE_CRITICAL_FRACTION = 0.95

# How much of an evidence string to keep. A stack trace runs to several
# kilobytes and the first frames are the ones that name the failure; the rest
# goes to the log bundle, which is what the other half of this application is
# for.
MAX_EVIDENCE_CHARS = 800

# XAPI message names that are worth reporting, and what they mean. Anything not
# here is routine — VM_STARTED, VM_SHUTDOWN, VM_MIGRATED and VM_SNAPSHOTTED
# alone were 3,381 of the 3,472 messages measured on one pool, and a report
# burying three real problems under them is a report nobody reads.
#
# The match is on the message name as XAPI emits it. Names not in this table
# are counted, and the count is reported, so a name that should be here is
# visible as "N other message types" rather than silently dropped.
MESSAGE_RULES: dict[str, tuple[str, str, str]] = {
    # name: (severity, title, suggested action)
    "HA_HOST_FAILED": (
        CRITICAL,
        "High availability fenced a host",
        "Find why the host stopped responding before re-enabling HA.",
    ),
    "HA_HOST_WAS_FENCED": (
        CRITICAL,
        "A host was fenced by high availability",
        "Find why the host stopped responding before re-enabling HA.",
    ),
    "HA_NETWORK_AGENT_LOST": (
        CRITICAL,
        "High availability lost the network agent",
        "Check the management network and the HA heartbeat SR.",
    ),
    "HA_STATEFILE_LOST": (
        CRITICAL,
        "High availability lost its statefile",
        "Check the heartbeat storage repository is reachable from every host.",
    ),
    "HOST_SYNC_DATA_FAILED": (
        WARNING,
        "A host failed to synchronise data",
        "Check the host is reachable and its clock is in step with the pool.",
    ),
    "SR_BACKEND_FAILURE": (
        CRITICAL,
        "A storage repository backend failed",
        "Check the SR is attached and its backing storage is reachable.",
    ),
    "SR_DISK_SPACE_LOW": (
        CRITICAL,
        "A storage repository is running out of space",
        "Free space on the SR or add capacity before it fills.",
    ),
    "VDI_CBT_METADATA_INCONSISTENT": (
        WARNING,
        "Changed Block Tracking metadata is inconsistent",
        "Delta backups of this disk will fall back to a full copy. "
        "Disable and re-enable CBT on the disk to rebuild it.",
    ),
    "POOL_MASTER_TRANSITION": (
        INFO,
        "The pool master changed",
        "Expected after a planned failover; unexpected transitions are worth "
        "correlating with host uptime.",
    ),
    "LICENSE_EXPIRED": (
        WARNING,
        "A licence has expired",
        "Renew the licence, or expect support and update access to stop.",
    ),
    "MULTIPATH_PERIODIC_ALERT": (
        WARNING,
        "Multipath reported a path change",
        "Check every path to the storage is up; a flapping path degrades "
        "throughput long before it fails outright.",
    ),
}

# Message names matched by prefix rather than exactly, for families a vendor
# adds to. Checked after the exact table above, longest prefix first, so an
# exact entry always wins.
#
# The title here is a *prefix* for the title, not the whole of it: the message
# name is appended, because one prefix covers several distinct conditions.
# Measured on one pool, ``twinstor_degraded`` (lost redundancy) and
# ``twinstor_ha_disarmed`` (no fencing, split-brain risk) are different
# problems with different remedies, and a shared title made them read as three
# copies of one finding.
MESSAGE_PREFIX_RULES: dict[str, tuple[str, str, str]] = {
    "twinstor_": (
        WARNING,
        "TwinStor",
        "Read the message body: TwinStor states its own remedy, and a "
        "degraded pool is running on a single copy of the data.",
    ),
}

# Task failures worth reporting, matched against the failure message. A failed
# login is not a pool fault and floods the list — measured, 13 of 16 failed
# tasks on one pool were "invalid credentials" from a browser session.
TASK_NOISE_PATTERNS = (
    re.compile(r"invalid credentials", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
)


@dataclass(frozen=True)
class Finding:
    """One problem, and what established it.

    ``evidence`` is already redacted by the time a Finding exists. Nothing
    constructs one from raw API text except ``_finding``, which redacts on the
    way in, so a Finding cannot carry an unmasked address by accident.
    """

    severity: str
    title: str
    evidence: str
    action: str
    source: str
    at: float | None = None
    count: int = 1
    object_id: str = ""

    @property
    def severity_rank(self) -> int:
        """Position in SEVERITIES, for sorting. Unknown severities sort last."""
        try:
            return SEVERITIES.index(self.severity)
        except ValueError:
            return len(SEVERITIES)

    @property
    def source_title(self) -> str:
        return SOURCE_TITLES.get(self.source, self.source)


@dataclass
class SourceResult:
    """What one source produced, or why it produced nothing.

    Read is not the same as empty and empty is not the same as refused. An
    operator looking at a report with no storage findings needs to know whether
    storage was checked, which is why a source that could not be read carries
    its reason rather than being absent.
    """

    name: str
    read: bool
    reason: str = ""
    examined: int = 0

    # What this source actually looked at, when the count alone does not say.
    # The patch check is the case that needs it: "0 patches" is the *answer*,
    # and which pools were asked is the thing that makes it trustworthy.
    detail: str = ""

    @property
    def info(self) -> SourceInfo:
        """This source's description, or a plain one for an unknown name."""
        return SOURCES.get(
            self.name,
            SourceInfo(title=self.name, origin="", holds=self.name, unit="records"),
        )

    @property
    def title(self) -> str:
        return self.info.title

    @property
    def origin(self) -> str:
        """Whether this came from the hosts or from Xen Orchestra itself."""
        return self.info.origin

    @property
    def examined_text(self) -> str:
        """The record count as a phrase, so ``0`` is never ambiguous."""
        if self.read and self.detail:
            return self.detail
        return self.info.examined_text(self.examined, self.read)


@dataclass
class Report:
    """Every finding one run produced, with what was and was not read."""

    findings: list[Finding] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)
    window_days: int = DEFAULT_WINDOW_DAYS
    created_at: float = 0.0

    # Redaction rules that were switched off when this ran, by title.
    #
    # Recorded because the evidence below is masked with whatever was on at the
    # time, and an unmasked address in a report is indistinguishable from a
    # value no rule ever looked for. This is the same distinction the redaction
    # report draws between a rule with no hits and a rule switched off — and it
    # matters more here, because a findings report is written to be pasted into
    # a support ticket.
    rules_disabled: list[str] = field(default_factory=list)

    # Set when a log bundle's archive ended before its terminator — the same
    # truncated-download case the redacted repack salvages. The findings above
    # are still real, from every member read before the break; this says the
    # bundle was not read in full so a clean report is not mistaken for one.
    truncated: bool = False

    @property
    def counts(self) -> dict[str, int]:
        """How many findings at each severity, including the zeros.

        Every severity appears whether or not anything reached it, because
        "no critical findings" is the answer someone opening this page wants
        and an absent row does not say it.
        """
        return {
            severity: sum(1 for f in self.findings if f.severity == severity)
            for severity in SEVERITIES
        }

    @property
    def unread_sources(self) -> list[SourceResult]:
        return [source for source in self.sources if not source.read]

    @property
    def is_clean(self) -> bool:
        return not self.findings


def collect_findings(
    client: XoClient,
    pools: list[Pool],
    *,
    enabled=None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: float | None = None,
    progress=None,
) -> Report:
    """Read every source and build the report.

    ``pools`` comes from the stored inventory rather than being fetched here,
    for the same reason the collect page takes its host list from there: the
    missing-patches route is per pool and needs an id, and the inventory is
    already what the operator saw.

    ``progress(percent, step)`` is the job's own reporter, passed straight
    through, so a findings run is cancellable at every source boundary without
    this module knowing what a job is.

    One source failing never fails the run. Each is tried, and a failure is
    recorded against that source with its reason.
    """
    moment = time.time() if now is None else now
    cutoff = moment - window_days * 86400
    report = Report(
        window_days=window_days,
        created_at=moment,
        rules_disabled=disabled_rule_titles(enabled),
    )

    steps = (
        (
            SOURCE_MESSAGES,
            10,
            "Reading XAPI messages",
            lambda: _from_messages(client, cutoff, enabled),
        ),
        (SOURCE_ALARMS, 25, "Reading alarms", lambda: _from_alarms(client, cutoff, enabled)),
        (SOURCE_TASKS, 40, "Reading tasks", lambda: _from_tasks(client, cutoff, enabled)),
        (SOURCE_PATCHES, 55, "Checking patches", lambda: _from_patches(client, pools, enabled)),
        (
            SOURCE_BACKUPS,
            70,
            "Reading backup runs",
            lambda: _from_backups(client, cutoff, moment, enabled),
        ),
        (
            SOURCE_RESTORES,
            80,
            "Reading restore runs",
            lambda: _from_restores(client, cutoff, enabled),
        ),
        (
            SOURCE_DASHBOARD,
            90,
            "Reading the pool dashboard",
            lambda: _from_dashboard(client, enabled),
        ),
    )

    for name, percent, step, read in steps:
        if progress is not None:
            progress(percent, step)
        try:
            result = read()
        except XoError as exc:
            report.sources.append(SourceResult(name, read=False, reason=str(exc)))
            continue
        # A source returns (findings, examined), or adds a third element when
        # the count alone does not describe what it looked at.
        findings, examined = result[0], result[1]
        detail = result[2] if len(result) > 2 else ""
        report.sources.append(SourceResult(name, read=True, examined=examined, detail=detail))
        report.findings.extend(findings)

    report.findings = sort_findings(report.findings)
    return report


def disabled_rule_titles(enabled) -> list[str]:
    """The titles of the redaction rules that are switched off.

    Titles rather than names because this is read by a person deciding whether
    a report is safe to send, and ``ipv4`` is not what the redaction page calls
    that rule. ``None`` means every rule is on, which is what ``redact_line``
    itself takes it to mean.
    """
    if enabled is None:
        return []
    return [rule.title for rule in RULES if rule.name not in enabled]


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Worst first, then most recent, then by title.

    Time descending within a severity because an operator reading a list of
    equally serious things wants the one happening now at the top. A finding
    with no time — a state rather than an event — sorts after the timed ones
    at its severity rather than being treated as infinitely old.
    """
    return sorted(
        findings,
        key=lambda f: (f.severity_rank, -(f.at or 0.0), f.title.lower()),
    )


LOG_FINDING_RULES: tuple[tuple[str, str, str, str, re.Pattern[str]], ...] = (
    (
        SOURCE_LOGS_MULTIPATH,
        WARNING,
        "Multipath reported a path failure",
        "Check every storage path and the switch ports serving it.",
        re.compile(r"multipath.*(?:fail|flap|down)|(?:path|checker).*fail", re.IGNORECASE),
    ),
    (
        SOURCE_LOGS_HA,
        CRITICAL,
        "High availability reported a fencing or heartbeat failure",
        "Check host reachability, the management network, and the HA heartbeat SR.",
        re.compile(r"(?:ha|heartbeat).*(?:fenc|fail|lost)|fenc(?:e|ed|ing)", re.IGNORECASE),
    ),
    (
        SOURCE_LOGS_STORAGE,
        CRITICAL,
        "The logs contain a storage failure",
        "Check the storage repository and its backing device before the failure spreads.",
        re.compile(
            r"(?:storage|storage repository|filesystem|file system|device-mapper|scsi|"
            r"\bsr[_.: -]).*(?:error|fail|full|read-only)|"
            r"(?:i/o error|no space left|read-only)",
            re.IGNORECASE,
        ),
    ),
    (
        SOURCE_LOGS_XAPI,
        WARNING,
        "The logs contain an XAPI exception",
        "Read the surrounding XAPI traceback and correlate it with the API findings.",
        re.compile(r"(?:xapi|xenopsd).*(?:error|exception|traceback)|backtrace", re.IGNORECASE),
    ),
)


def collect_log_findings(bundle_path, *, enabled=None, progress=None) -> Report:
    """Inspect a collected tar bundle without contacting Xen Orchestra.

    ``progress(percent, step)``, when given, is called once per archive member
    scanned so a long analysis moves rather than sitting at one value until the
    whole tar has been read. It is driven off bytes consumed against the
    bundle's own file size rather than a member count, because counting members
    first means reading the archive twice. Every call is also a cancellation
    checkpoint, the same as it is in ``collect_findings``.

    A bundle that arrives truncated — the same Nginx Proxy Manager issue the
    redacted repack already salvages — is not a lost analysis: everything read
    before the break is real log data. What was recovered is kept and
    ``Report.truncated`` says so, rather than the whole run failing on the
    ``EOFError`` the last few missing bytes raise. Nothing readable at all is a
    different matter and still raises.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    examined = {source: 0 for source, *_ in LOG_FINDING_RULES}
    total_bytes = bundle_path.stat().st_size or 1
    members = 0
    truncated = False

    try:
        # Streaming mode, not random access: a bundle whose end is missing
        # cannot be indexed, and only reading it strictly in order lets every
        # intact member before the break be read rather than failing on the
        # first seek past it — the same reasoning job_collect's repack follows.
        with tarfile.open(bundle_path, mode="r|*") as bundle:
            for member in bundle:
                members += 1
                if member.isfile():
                    handle = bundle.extractfile(member)
                    if handle is not None:
                        for raw_line in handle:
                            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                            for source, severity, title, action, pattern in LOG_FINDING_RULES:
                                if not pattern.search(line):
                                    continue
                                examined[source] += 1
                                # Log lines usually carry a timestamp, process id, or
                                # changing object detail. Those values make identical
                                # failures look different, so group by the detected
                                # condition and keep the latest matching line as evidence.
                                key = source
                                item = grouped.get(key)
                                if item is None:
                                    grouped[key] = {
                                        "severity": severity,
                                        "title": title,
                                        "evidence": line,
                                        "action": action,
                                        "source": source,
                                        "count": 1,
                                    }
                                else:
                                    item["count"] += 1
                                    item["evidence"] = line
                if progress is not None:
                    consumed = min(member.offset_data + member.size, total_bytes)
                    progress(int(10 + 80 * consumed / total_bytes), f"Scanning {member.name}")
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error):
        if members == 0:
            raise
        truncated = True

    findings = [_finding(enabled=enabled, **item) for item in grouped.values()]
    sources = [
        SourceResult(
            source,
            read=True,
            examined=examined[source],
            detail=(
                f"{examined[source]:,} matching line(s)"
                if examined[source]
                else "checked - no matching lines"
            ),
        )
        for source, *_ in LOG_FINDING_RULES
    ]
    return Report(
        findings=sort_findings(findings),
        sources=sources,
        window_days=0,
        created_at=time.time(),
        rules_disabled=disabled_rule_titles(enabled),
        truncated=truncated,
    )


# -- sources -------------------------------------------------------------


def _from_messages(client: XoClient, cutoff: float, enabled) -> tuple[list[Finding], int]:
    """Findings from XAPI messages, grouped so a repeat is one row with a count."""
    records = client.messages(cutoff)
    return _classify_events(records, cutoff, enabled, SOURCE_MESSAGES), len(records)


def _from_alarms(client: XoClient, cutoff: float, enabled) -> tuple[list[Finding], int]:
    """Findings from alarms.

    An alarm XO has not matched to a rule is still reported, at warning, rather
    than dropped: XO raised it deliberately, so an unrecognised one is more
    likely to matter than an unrecognised message.
    """
    records = client.alarms(cutoff)
    findings = _classify_events(records, cutoff, enabled, SOURCE_ALARMS, report_unknown=True)
    return findings, len(records)


def _classify_events(
    records: list[dict[str, Any]],
    cutoff: float,
    enabled,
    source: str,
    *,
    report_unknown: bool = False,
) -> list[Finding]:
    """Turn message-shaped records into findings, one row per repeated event.

    Grouping is by name and object: four ``twinstor_degraded`` messages about
    the same SR are one finding that happened four times, not four findings.
    The evidence and time kept are the most recent occurrence's, because that
    is the state the pool is in now.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for record in records:
        at = _seconds(record.get("time"))
        if at is None or at < cutoff:
            continue

        name = str(record.get("name") or "").strip()
        if not name:
            continue

        rule = _message_rule(name)
        if rule is None:
            if not report_unknown:
                continue
            rule = (
                WARNING,
                f"Alarm: {name}",
                "Xen Orchestra raised this alarm and XCP Pulse has no rule for "
                "it. Read the body and check the object it names.",
            )

        object_id = str(record.get("$object") or "")
        key = (name, object_id)
        previous = grouped.get(key)
        if previous is not None and previous["at"] >= at:
            previous["count"] += 1
            continue

        grouped[key] = {
            "rule": rule,
            "at": at,
            "body": str(record.get("body") or ""),
            "object_id": object_id,
            "name": name,
            "count": (previous["count"] + 1) if previous else 1,
        }

    findings = []
    for item in grouped.values():
        severity, title, action = item["rule"]
        findings.append(
            _finding(
                severity=severity,
                title=title,
                evidence=item["body"] or f"{item['name']} on {item['object_id'] or 'the pool'}",
                action=action,
                source=source,
                enabled=enabled,
                at=item["at"],
                count=item["count"],
                object_id=item["object_id"],
            )
        )
    return findings


def _message_rule(name: str) -> tuple[str, str, str] | None:
    """The rule for one message name, exact match first then longest prefix."""
    exact = MESSAGE_RULES.get(name)
    if exact is not None:
        return exact
    for prefix in sorted(MESSAGE_PREFIX_RULES, key=len, reverse=True):
        if name.startswith(prefix):
            severity, title, action = MESSAGE_PREFIX_RULES[prefix]
            # The name completes the title, so two conditions under one prefix
            # stay two findings rather than reading as duplicates of one.
            return severity, f"{title}: {name}", action
    return None


def _from_tasks(client: XoClient, cutoff: float, enabled) -> tuple[list[Finding], int]:
    """Findings from failed XO tasks.

    Failed logins are dropped. They are the largest group on a real instance
    and say nothing about the pool — a report listing thirteen bad-password
    attempts above one storage fault has buried the finding that matters.

    Only ``properties.name`` and the failure message are read out of a task.
    The rest of ``properties`` carries request arguments — measured: usernames
    and client IP addresses — and nothing needs them.
    """
    records = client.tasks(cutoff)
    grouped: dict[str, dict[str, Any]] = {}

    for record in records:
        if str(record.get("status") or "").lower() != "failure":
            continue

        at = _millis(record.get("end")) or _millis(record.get("start"))
        if at is None or at < cutoff:
            continue

        message = _task_message(record)
        if not message or any(pattern.search(message) for pattern in TASK_NOISE_PATTERNS):
            continue

        properties = record.get("properties")
        name = ""
        if isinstance(properties, dict):
            name = str(properties.get("name") or "")

        key = f"{name}\x00{message}"
        previous = grouped.get(key)
        if previous is not None:
            previous["count"] += 1
            previous["at"] = max(previous["at"], at)
            continue

        grouped[key] = {"name": name, "message": message, "at": at, "count": 1}

    findings = [
        _finding(
            severity=WARNING,
            title=f"Task failed: {item['name']}" if item["name"] else "A Xen Orchestra task failed",
            evidence=item["message"],
            action=(
                "Repeat the operation and watch the task in Xen Orchestra, or "
                "collect the host's logs for what XAPI recorded at that moment."
            ),
            source=SOURCE_TASKS,
            enabled=enabled,
            at=item["at"],
            count=item["count"],
        )
        for item in grouped.values()
    ]
    return findings, len(records)


def _task_message(record: dict[str, Any]) -> str:
    """The failure message from a task's result.

    ``result`` is an object for a thrown error and a bare value otherwise, so
    both shapes are handled — a task whose result is a string is not malformed,
    it just failed differently.
    """
    result = record.get("result")
    if isinstance(result, dict):
        message = str(result.get("message") or "").strip()
        if message:
            return message
        return str(result.get("name") or "").strip()
    if isinstance(result, str):
        return result.strip()
    return ""


def _from_patches(client: XoClient, pools: list[Pool], enabled) -> tuple[list[Finding], int, str]:
    """Findings from the per-pool missing-patch list.

    These are **XCP-ng host updates** — the ones applied from Xen Orchestra's
    Patches tab — not Xen Orchestra's own version.

    Raises XoError if *every* pool refuses, so the source is recorded as
    unavailable rather than as read-and-clean — an XOA without a subscription
    would otherwise report "no missing patches", which is a lie of exactly the
    kind this application exists to avoid.

    Returns a third element saying which pools were asked, because "0 patches"
    is the answer here rather than an absence of data, and a bare zero cannot
    be told apart from having checked nothing.
    """
    if not pools:
        raise XoError("No pools in the stored inventory. Refresh the inventory first.")

    findings: list[Finding] = []
    examined = 0
    refusals: list[str] = []
    checked: list[str] = []

    for pool in pools:
        try:
            patches = client.missing_patches(pool.id)
        except XoError as exc:
            refusals.append(str(exc))
            continue

        checked.append(pool.name)
        examined += len(patches)
        if not patches:
            continue

        names = [str(patch.get("name") or patch.get("id") or "?") for patch in patches]
        findings.append(
            _finding(
                severity=WARNING,
                title=f"{len(patches)} patch(es) missing on {pool.name}",
                evidence=", ".join(names[:20]),
                action=(
                    "Apply the updates from Xen Orchestra, host by host, with the pool master last."
                ),
                source=SOURCE_PATCHES,
                enabled=enabled,
                object_id=pool.id,
                count=len(patches),
            )
        )

    if refusals and not findings and examined == 0:
        raise XoError(refusals[0])

    pool_list = ", ".join(checked)
    if examined:
        detail = f"{examined:,} missing on {pool_list}"
    else:
        detail = f"none missing - {pool_list} up to date"
    return findings, examined, detail


def _from_backups(
    client: XoClient, cutoff: float, now: float, enabled
) -> tuple[list[Finding], int]:
    """Findings from backup runs: failures, and jobs that have stopped running.

    A job whose last run failed and a job that has not run at all are different
    problems with the same consequence, so both are reported. The second is the
    one nobody notices, because every dashboard it appears on looks green.
    """
    records = client.backup_logs(cutoff)
    findings = _failed_runs(records, cutoff, enabled, SOURCE_BACKUPS, "Backup")
    findings.extend(_stale_backups(records, now, enabled))
    return findings, len(records)


def _from_restores(client: XoClient, cutoff: float, enabled) -> tuple[list[Finding], int]:
    """Findings from restore runs.

    A failed restore is reported at critical rather than warning: a backup that
    cannot be restored is not a backup, and it is the one failure discovered at
    the worst possible moment.
    """
    records = client.restore_logs(cutoff)
    findings = _failed_runs(records, cutoff, enabled, SOURCE_RESTORES, "Restore", severity=CRITICAL)
    return findings, len(records)


def _failed_runs(
    records: list[dict[str, Any]],
    cutoff: float,
    enabled,
    source: str,
    noun: str,
    *,
    severity: str = WARNING,
) -> list[Finding]:
    """One finding per job whose runs failed, counting the failures."""
    grouped: dict[str, dict[str, Any]] = {}

    for record in records:
        status = str(record.get("status") or "").lower()
        if status not in ("failure", "error", "interrupted"):
            continue

        at = _millis(record.get("end")) or _millis(record.get("start"))
        if at is None or at < cutoff:
            continue

        name = str(record.get("jobName") or record.get("jobId") or "unnamed job")
        item = grouped.setdefault(name, {"at": at, "count": 0, "status": status})
        item["count"] += 1
        if at > item["at"]:
            item["at"] = at
            item["status"] = status

    return [
        _finding(
            severity=severity,
            title=f"{noun} job failed: {name}",
            evidence=f"{item['count']} run(s) ended '{item['status']}' within the window.",
            action=(
                f"Open the {noun.lower()} job in Xen Orchestra and read the failed "
                f"run's tasks — the failing step names the VM or the remote."
            ),
            source=source,
            enabled=enabled,
            at=item["at"],
            count=item["count"],
        )
        for name, item in grouped.items()
    ]


def _stale_backups(records: list[dict[str, Any]], now: float, enabled) -> list[Finding]:
    """Jobs whose most recent run is older than BACKUP_STALE_DAYS.

    Deliberately computed from the run history rather than from the job list:
    a job deleted in XO leaves its runs behind, and reporting a deleted job as
    stale would be noise. A job that still exists and has stopped firing has
    recent runs for every other job around it and none of its own.
    """
    latest: dict[str, float] = {}
    for record in records:
        at = _millis(record.get("start"))
        if at is None:
            continue
        name = str(record.get("jobName") or record.get("jobId") or "unnamed job")
        latest[name] = max(latest.get(name, 0.0), at)

    threshold = now - BACKUP_STALE_DAYS * 86400
    return [
        _finding(
            severity=WARNING,
            title=f"Backup job has not run recently: {name}",
            evidence=f"Last run {_age(now - at)} ago.",
            action=(
                "Check the job's schedule is enabled in Xen Orchestra. A job that "
                "stopped firing looks healthy everywhere except here."
            ),
            source=SOURCE_BACKUPS,
            enabled=enabled,
            at=at,
        )
        for name, at in latest.items()
        if at < threshold
    ]


def _from_dashboard(client: XoClient, enabled) -> tuple[list[Finding], int]:
    """Findings from the totals XO has already computed.

    This is the one administrator-only source. It is also the only one
    reporting *states* rather than events — hosts halted now, storage full
    now — so nothing here is filtered by the time window.
    """
    payload = client.pool_dashboard()
    findings: list[Finding] = []

    findings.extend(_dashboard_patches(payload, enabled))
    findings.extend(_dashboard_backups(payload, enabled))
    findings.extend(_dashboard_storage(payload, enabled))
    findings.extend(_dashboard_hosts(payload, enabled))

    return findings, len(payload)


def _dashboard_patches(payload: dict[str, Any], enabled) -> list[Finding]:
    """Pools with missing patches, and hosts past end of life."""
    section = payload.get("missingPatches")
    if not isinstance(section, dict):
        return []

    findings = []
    pools_behind = _as_int(section.get("nPoolsWithMissingPatches"))
    hosts_behind = _as_int(section.get("nHostsWithMissingPatches"))
    if pools_behind or hosts_behind:
        findings.append(
            _finding(
                severity=WARNING,
                title=f"{hosts_behind} host(s) have patches available",
                evidence=f"{pools_behind} pool(s) and {hosts_behind} host(s) are behind.",
                action="Apply the updates from Xen Orchestra, with the pool master last.",
                source=SOURCE_DASHBOARD,
                enabled=enabled,
            )
        )

    failed = _as_int(section.get("nHostsFailed"))
    if failed:
        findings.append(
            _finding(
                severity=WARNING,
                title=f"{failed} host(s) could not be checked for patches",
                evidence="Xen Orchestra could not read the update state for these hosts.",
                action=(
                    "Check the hosts are reachable and their update repositories are configured."
                ),
                source=SOURCE_DASHBOARD,
                enabled=enabled,
            )
        )

    eol = section.get("nHostsEol")
    eol_count = _as_int(eol.get("nHosts")) if isinstance(eol, dict) else _as_int(eol)
    if eol_count:
        findings.append(
            _finding(
                severity=CRITICAL,
                title=f"{eol_count} host(s) are running an end-of-life version",
                evidence="Xen Orchestra reports these hosts past their supported life.",
                action="Upgrade to a supported XCP-ng release; an EOL host gets no security fixes.",
                source=SOURCE_DASHBOARD,
                enabled=enabled,
            )
        )

    if not isinstance(section.get("hasAuthorization"), bool) or section["hasAuthorization"]:
        return findings

    findings.append(
        _finding(
            severity=INFO,
            title="Patch state could not be read",
            evidence="Xen Orchestra reports no authorization to check for updates.",
            action=(
                "On XOA this needs a support subscription; on XO from the sources it needs none."
            ),
            source=SOURCE_DASHBOARD,
            enabled=enabled,
        )
    )
    return findings


def _dashboard_backups(payload: dict[str, Any], enabled) -> list[Finding]:
    """Backup job health and VM protection, as XO totals them."""
    section = payload.get("backups")
    if not isinstance(section, dict):
        return []

    findings = []
    jobs = section.get("jobs")
    if isinstance(jobs, dict):
        failed = _as_int(jobs.get("failed"))
        if failed:
            findings.append(
                _finding(
                    severity=WARNING,
                    title=f"{failed} backup job(s) are failing",
                    evidence=(
                        f"{failed} of {_as_int(jobs.get('total'))} jobs last ended in failure."
                    ),
                    action="Open each failing job in Xen Orchestra and read its most recent run.",
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )

        no_recent = _as_int(jobs.get("noRecentRun"))
        if no_recent:
            findings.append(
                _finding(
                    severity=WARNING,
                    title=f"{no_recent} backup job(s) have not run recently",
                    evidence="Xen Orchestra reports these jobs with no recent run.",
                    action="Check each job's schedule is enabled.",
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )

    protection = section.get("vmsProtection")
    if isinstance(protection, dict):
        unprotected = _as_int(protection.get("unprotected"))
        not_in_job = _as_int(protection.get("notInJob"))
        if unprotected:
            findings.append(
                _finding(
                    severity=WARNING,
                    title=f"{unprotected} VM(s) are in a backup job that is not protecting them",
                    evidence="Xen Orchestra reports these VMs as unprotected.",
                    action="Check the job covering them is enabled and its last run succeeded.",
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )
        if not_in_job:
            findings.append(
                _finding(
                    severity=INFO,
                    title=f"{not_in_job} VM(s) are in no backup job",
                    evidence="Xen Orchestra reports these VMs as belonging to no job.",
                    action=(
                        "Expected for scratch and template VMs; anything else here "
                        "has no backup at all."
                    ),
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )

    return findings


def _dashboard_storage(payload: dict[str, Any], enabled) -> list[Finding]:
    """Storage and backup-repository headroom."""
    findings = []
    for key, label in (
        ("storageRepositories", "Storage repositories"),
        ("backupRepositories", "Backup repositories"),
    ):
        section = payload.get(key)
        size = _find_size(section)
        if size is None:
            continue

        total = _as_int(size.get("total"))
        used = _as_int(size.get("used"))
        if total <= 0:
            continue

        fraction = used / total
        if fraction < STORAGE_WARN_FRACTION:
            continue

        severity = CRITICAL if fraction >= STORAGE_CRITICAL_FRACTION else WARNING
        findings.append(
            _finding(
                severity=severity,
                title=f"{label} are {fraction * 100:.0f}% full",
                evidence=f"{_gib(used)} used of {_gib(total)}.",
                action=(
                    "Free space or add capacity. Thin-provisioned storage that "
                    "fills stops VMs rather than slowing them."
                ),
                source=SOURCE_DASHBOARD,
                enabled=enabled,
            )
        )
    return findings


def _find_size(section: object) -> dict[str, Any] | None:
    """The ``size`` mapping from a dashboard storage section.

    XO nests it one level deeper for backup repositories, keyed by remote type
    — measured: ``backupRepositories.other.size`` — so both shapes are read
    rather than assuming the flatter one.
    """
    if not isinstance(section, dict):
        return None
    size = section.get("size")
    if isinstance(size, dict):
        return size
    for value in section.values():
        if isinstance(value, dict) and isinstance(value.get("size"), dict):
            return value["size"]
    return None


def _dashboard_hosts(payload: dict[str, Any], enabled) -> list[Finding]:
    """Hosts and pools that are not in a healthy state right now."""
    findings = []

    hosts = payload.get("hostsStatus")
    if isinstance(hosts, dict):
        disabled = _as_int(hosts.get("disabled"))
        if disabled:
            findings.append(
                _finding(
                    severity=WARNING,
                    title=f"{disabled} host(s) are disabled",
                    evidence=f"{disabled} of {_as_int(hosts.get('total'))} hosts are disabled.",
                    action=(
                        "Expected during maintenance. A host left disabled takes no "
                        "VMs and silently halves the pool's capacity."
                    ),
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )
        unknown = _as_int(hosts.get("unknown"))
        if unknown:
            findings.append(
                _finding(
                    severity=CRITICAL,
                    title=f"{unknown} host(s) are in an unknown state",
                    evidence="Xen Orchestra cannot determine the state of these hosts.",
                    action="Check the hosts are up and the pool master can reach them.",
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )

    pools = payload.get("poolsStatus")
    if isinstance(pools, dict):
        for key, severity, phrase in (
            ("disconnected", CRITICAL, "disconnected"),
            ("unreachable", CRITICAL, "unreachable"),
        ):
            count = _as_int(pools.get(key))
            if not count:
                continue
            findings.append(
                _finding(
                    severity=severity,
                    title=f"{count} pool(s) are {phrase}",
                    evidence=f"Xen Orchestra reports {count} pool(s) as {phrase}.",
                    action=(
                        "Check the pool master is up and its credentials in "
                        "Xen Orchestra are current."
                    ),
                    source=SOURCE_DASHBOARD,
                    enabled=enabled,
                )
            )

    return findings


# -- helpers -------------------------------------------------------------


def _finding(
    *,
    severity: str,
    title: str,
    evidence: str,
    action: str,
    source: str,
    enabled,
    at: float | None = None,
    count: int = 1,
    object_id: str = "",
) -> Finding:
    """Build a Finding, redacting and truncating the evidence.

    The single constructor, so masking cannot be skipped on one path. Evidence
    is trimmed *after* redaction: cutting first could leave half a placeholder
    on the end, and cutting a masked string can never re-expose what was
    masked.
    """
    masked = redact_line(evidence, enabled)
    if len(masked) > MAX_EVIDENCE_CHARS:
        masked = masked[:MAX_EVIDENCE_CHARS].rstrip() + "…"

    return Finding(
        severity=severity,
        title=title,
        evidence=masked,
        action=action,
        source=source,
        at=at,
        count=count,
        object_id=object_id,
    )


def _seconds(value: object) -> float | None:
    """A XAPI timestamp, which is seconds, as a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _millis(value: object) -> float | None:
    """An XO timestamp, which is milliseconds, as seconds.

    XAPI messages carry seconds and XO tasks carry milliseconds — measured on
    the same instance, ``1783217728`` against ``1788552365766``. Mixing the two
    would put every task a thousand lifetimes in the future and silently pass
    every window check.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _as_int(value: object) -> int:
    """A dashboard count as an int, or 0 when absent or malformed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _gib(size: int) -> str:
    """A byte count in GiB, for a one-line piece of evidence."""
    return f"{size / 1024**3:.1f} GiB"


def _age(seconds: float) -> str:
    """A duration as something to read in a sentence."""
    days = seconds / 86400
    if days >= 2:
        return f"{days:.0f} days"
    hours = seconds / 3600
    if hours >= 2:
        return f"{hours:.0f} hours"
    return f"{max(1, int(seconds // 60))} minutes"
