"""The findings engine: what becomes a finding, and what deliberately does not.

The records here are shaped from real ones observed against a live XO CE
instance, because the two things easiest to get wrong are invisible in the
documentation: XAPI messages carry **seconds** while XO tasks and backup runs
carry **milliseconds**, and a routine VM lifecycle event outnumbers a real
problem by roughly a thousand to one.

Every condition is tested from both sides. A test asserting only that a finding
appears cannot catch a rule that fires for everything, which is exactly how a
warning stuck permanently on gets shipped.
"""

from __future__ import annotations

import os
import tarfile
import time
from datetime import UTC, datetime

import pytest

from app.findings import (
    CRITICAL,
    INFO,
    WARNING,
    Finding,
    Report,
    SourceResult,
    collect_findings,
    collect_log_findings,
    correlate_reports,
)
from app.xo_client import Pool, XoError

NOW = 1788700000.0
POOL = Pool(id="pool-1", name="Pool1")


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _message(name: str, *, body: str = "", ago_days: float = 1.0, obj: str = "obj-1") -> dict:
    """One XAPI message. ``time`` is in seconds, as XAPI emits it."""
    return {
        "id": f"msg-{name}-{obj}-{ago_days}",
        "name": name,
        "body": body,
        "time": NOW - ago_days * 86400,
        "$object": obj,
    }


def _task(*, status: str, message: str, name: str = "API call: vm.start", ago_days: float = 1.0):
    """One XO task. ``start``/``end`` are in milliseconds, as XO emits them."""
    at = (NOW - ago_days * 86400) * 1000
    return {
        "id": f"task-{name}-{message}-{ago_days}",
        "status": status,
        "start": at,
        "end": at + 100,
        "result": {"message": message, "name": "XoError", "stack": "XoError: " + message},
        "properties": {"name": name, "credentials": {"username": "nick"}},
    }


def _backup(*, status: str, job_name: str, ago_days: float = 1.0) -> dict:
    at = (NOW - ago_days * 86400) * 1000
    return {"id": f"b-{job_name}-{ago_days}", "jobName": job_name, "status": status, "start": at}


class FakeXo:
    """An XoClient stand-in returning whatever a test wants from each source.

    A source raising ``XoError`` is how a refusal is simulated, because that is
    what the real client does — every one of these routes answers a restricted
    account with either 200 and an empty list or a 403, and the difference is
    the thing under test.
    """

    def __init__(self, **sources: object) -> None:
        self._sources = sources

    def _get(self, key: str, default: object):
        value = self._sources.get(key, default)
        if isinstance(value, Exception):
            raise value
        return value

    def messages(self, since: float) -> list[dict]:
        return self._get("messages", [])

    def alarms(self, since: float) -> list[dict]:
        return self._get("alarms", [])

    def tasks(self, since: float) -> list[dict]:
        return self._get("tasks", [])

    def backup_logs(self, since: float) -> list[dict]:
        return self._get("backup_logs", [])

    def restore_logs(self, since: float) -> list[dict]:
        return self._get("restore_logs", [])

    def missing_patches(self, pool_id: str) -> list[dict]:
        return self._get("missing_patches", [])

    def pool_dashboard(self) -> dict:
        return self._get("dashboard", {})


def _run(**sources: object):
    return collect_findings(FakeXo(**sources), [POOL], now=NOW)


def _titles(report) -> list[str]:
    return [finding.title for finding in report.findings]


# -- messages ------------------------------------------------------------


def test_a_storage_message_becomes_a_critical_finding() -> None:
    report = _run(messages=[_message("SR_BACKEND_FAILURE", body="nfs mount failed")])

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.severity == CRITICAL
    assert finding.source == "messages"
    assert "nfs mount failed" in finding.evidence
    assert finding.action
    # Tagged with its condition family at the point the rule fires, rather
    # than left for correlate_reports to re-derive from title/evidence text.
    assert finding.family == "storage"


def test_a_licence_message_has_no_family_and_falls_back_to_text_matching() -> None:
    """Not every message name maps to a log-side condition family — a licence
    expiring has no log-side echo — so it is left untagged rather than forced
    into one, and correlation for it (if any) still goes through the text
    match `_finding_family` falls back to."""
    report = _run(messages=[_message("LICENSE_EXPIRED")])

    assert len(report.findings) == 1
    assert report.findings[0].family is None


def test_routine_vm_lifecycle_messages_produce_no_findings() -> None:
    """The other side of the rule above, and the reason the report is readable.

    Measured on one pool: VM_SNAPSHOTTED, VM_STARTED, VM_SHUTDOWN and
    VM_MIGRATED were 3,381 of 3,472 messages. A report including them buries
    every real problem, so their absence is as much the behaviour as the
    presence of the finding above.
    """
    report = _run(
        messages=[
            _message("VM_STARTED", body="VM 'XOA' started"),
            _message("VM_SHUTDOWN"),
            _message("VM_MIGRATED"),
            _message("VM_SNAPSHOTTED"),
            _message("VM_REBOOTED"),
        ]
    )

    assert report.findings == []
    assert report.is_clean
    # The source was still read — "nothing found" is not "not checked".
    examined = {source.name: source.examined for source in report.sources}
    assert examined["messages"] == 5


def test_repeats_of_one_message_are_one_finding_with_a_count() -> None:
    report = _run(
        messages=[
            _message("SR_DISK_SPACE_LOW", body="old", ago_days=3),
            _message("SR_DISK_SPACE_LOW", body="newer", ago_days=2),
            _message("SR_DISK_SPACE_LOW", body="newest", ago_days=1),
        ]
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.count == 3
    # The most recent occurrence is the state the pool is in now.
    assert finding.evidence == "newest"


def test_the_same_message_on_different_objects_stays_separate() -> None:
    """Two SRs failing is two problems, not one that happened twice."""
    report = _run(
        messages=[
            _message("SR_BACKEND_FAILURE", body="sr A", obj="sr-a"),
            _message("SR_BACKEND_FAILURE", body="sr B", obj="sr-b"),
        ]
    )

    assert len(report.findings) == 2
    assert {finding.count for finding in report.findings} == {1}


def test_a_message_older_than_the_window_is_not_reported() -> None:
    """A pool that recovered a year ago is not currently degraded."""
    report = _run(messages=[_message("SR_BACKEND_FAILURE", body="ancient", ago_days=400)])

    assert report.findings == []


def test_a_vendor_prefix_keeps_the_message_name_in_the_title() -> None:
    """Two conditions under one prefix must not read as duplicates of one.

    Measured: ``twinstor_degraded`` (lost redundancy) and
    ``twinstor_ha_disarmed`` (no fencing) are different problems with different
    remedies. A shared title made three findings look like three copies of one.
    """
    report = _run(
        messages=[
            _message("twinstor_degraded", body="single copy", obj="sr-a"),
            _message("twinstor_ha_disarmed", body="no fencing", obj="sr-a"),
        ]
    )

    titles = _titles(report)
    assert len(set(titles)) == 2
    assert any("twinstor_degraded" in title for title in titles)
    assert any("twinstor_ha_disarmed" in title for title in titles)


def test_an_unknown_message_name_is_dropped_but_an_unknown_alarm_is_reported() -> None:
    """The two sources are treated differently, deliberately.

    XO raises an alarm on purpose, so one it has no rule for is more likely to
    matter than an unrecognised message — of which there are thousands.
    """
    report = _run(
        messages=[_message("SOME_NEW_XAPI_EVENT", body="who knows")],
        alarms=[_message("some_new_alarm", body="a real alarm")],
    )

    titles = _titles(report)
    assert titles == ["Alarm: some_new_alarm"]
    assert report.findings[0].source == "alarms"


# -- tasks ---------------------------------------------------------------


def test_a_failed_task_becomes_a_finding_naming_the_operation() -> None:
    report = _run(tasks=[_task(status="failure", message="VM_BAD_POWER_STATE", name="vm.delete")])

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.severity == WARNING
    assert "vm.delete" in finding.title
    assert "VM_BAD_POWER_STATE" in finding.evidence


def test_a_successful_task_produces_no_finding() -> None:
    report = _run(tasks=[dict(_task(status="success", message=""), result=None)])

    assert report.findings == []


def test_failed_logins_are_not_reported_as_pool_problems() -> None:
    """Measured: 13 of 16 failed tasks on one pool were bad passwords.

    They say nothing about the pool, and a report listing them above a storage
    fault has buried the finding that matters.
    """
    report = _run(
        tasks=[
            _task(status="failure", message="invalid credentials", name="XO user authentication"),
            _task(
                status="failure",
                message="authentication failed",
                name="API call: session.signIn",
            ),
        ]
    )

    assert report.findings == []


def test_task_evidence_never_carries_the_credentials_from_properties() -> None:
    """XO task ``properties`` carry a username and the caller's IP address.

    A findings report is a thing an operator sends to Vates, so only the task's
    name and its failure message are read out of it — never ``properties``
    wholesale.
    """
    report = _run(tasks=[_task(status="failure", message="something broke")])

    finding = report.findings[0]
    assert "nick" not in finding.evidence
    assert "credentials" not in finding.evidence


def test_a_task_timestamp_is_read_as_milliseconds() -> None:
    """The scale bug that would silently pass every window check.

    A task 400 days old must fall outside a 30-day window. Read as seconds
    instead of milliseconds its timestamp lands thousands of years in the
    future, which passes every comparison and reports ancient failures as
    current.
    """
    recent = _run(tasks=[_task(status="failure", message="recent", ago_days=1)])
    old = _run(tasks=[_task(status="failure", message="old", ago_days=400)])

    assert len(recent.findings) == 1
    assert old.findings == []


# -- backups and restores ------------------------------------------------


def test_a_failed_backup_job_is_reported_and_a_successful_one_is_not() -> None:
    report = _run(
        backup_logs=[
            _backup(status="failure", job_name="Delta Backup"),
            _backup(status="success", job_name="XO Config Backup"),
        ]
    )

    titles = _titles(report)
    assert titles == ["Backup job failed: Delta Backup"]


def test_a_failed_restore_outranks_a_failed_backup() -> None:
    """A backup that cannot be restored is not a backup."""
    report = _run(
        backup_logs=[_backup(status="failure", job_name="Nightly")],
        restore_logs=[_backup(status="failure", job_name="Nightly")],
    )

    by_severity = {finding.severity: finding.title for finding in report.findings}
    assert by_severity[CRITICAL].startswith("Restore job failed")
    assert by_severity[WARNING].startswith("Backup job failed")


def test_a_job_that_stopped_running_is_reported_even_though_it_succeeded() -> None:
    """The failure nobody notices: every dashboard showing it looks green."""
    report = _run(
        backup_logs=[
            _backup(status="success", job_name="Weekly", ago_days=20),
            _backup(status="success", job_name="Nightly", ago_days=1),
        ]
    )

    titles = _titles(report)
    assert titles == ["Backup job has not run recently: Weekly"]


# -- patches and the dashboard -------------------------------------------


def test_missing_patches_are_reported_per_pool() -> None:
    report = _run(missing_patches=[{"name": "XS83E001"}, {"name": "XS83E002"}])

    assert _titles(report) == ["2 patch(es) missing on Pool1"]
    assert "XS83E001" in report.findings[0].evidence


def test_a_refused_patch_route_is_recorded_as_unread_not_as_clean() -> None:
    """An XOA without a subscription must not report "no missing patches".

    Reporting a refusal as a clean result is the exact lie this application
    exists to avoid, so the source is marked unread and the page says so.
    """
    report = _run(missing_patches=XoError("needs a support subscription"))

    assert report.findings == []
    unread = {source.name: source.reason for source in report.unread_sources}
    assert "subscription" in unread["patches"]


def test_a_full_storage_repository_is_reported_and_a_roomy_one_is_not() -> None:
    full = _run(dashboard={"storageRepositories": {"size": {"total": 1000, "used": 970}}})
    roomy = _run(dashboard={"storageRepositories": {"size": {"total": 1000, "used": 100}}})

    assert full.findings[0].severity == CRITICAL
    assert roomy.findings == []


def test_backup_repository_size_is_found_under_its_remote_type() -> None:
    """XO nests it one level deeper here — measured: backupRepositories.other.size."""
    report = _run(dashboard={"backupRepositories": {"other": {"size": {"total": 100, "used": 90}}}})

    assert _titles(report) == ["Backup repositories are 90% full"]


def test_dashboard_host_and_patch_state_becomes_findings() -> None:
    report = _run(
        dashboard={
            "missingPatches": {"nPoolsWithMissingPatches": 1, "nHostsWithMissingPatches": 2},
            "hostsStatus": {"disabled": 1, "total": 3},
            "poolsStatus": {"disconnected": 1},
        }
    )

    titles = _titles(report)
    assert "2 host(s) have patches available" in titles
    assert "1 host(s) are disabled" in titles
    assert "1 pool(s) are disconnected" in titles


def test_a_healthy_dashboard_produces_no_findings() -> None:
    """The whole dashboard, all green — measured shape from a live instance."""
    report = _run(
        dashboard={
            "missingPatches": {
                "hasAuthorization": True,
                "nHostsFailed": 0,
                "nHostsWithMissingPatches": 0,
                "nPoolsWithMissingPatches": 0,
                "nHostsEol": {"isEmpty": True},
            },
            "hostsStatus": {"disabled": 0, "running": 2, "unknown": 0, "total": 2},
            "poolsStatus": {"connected": 1, "disconnected": 0, "unreachable": 0},
            "backups": {
                "jobs": {"failed": 0, "noRecentRun": 0, "total": 2},
                "vmsProtection": {"protected": 7, "unprotected": 0, "notInJob": 0},
            },
            "storageRepositories": {"size": {"total": 1000, "used": 10}},
        }
    )

    assert report.findings == []
    assert report.is_clean


# -- the run as a whole --------------------------------------------------


def test_one_refused_source_does_not_fail_the_run() -> None:
    """A restricted account is refused the dashboard and can still read messages.

    A report covering the sources that answered is worth far more than an
    error, so the run continues and records which one was refused.
    """
    report = _run(
        messages=[_message("SR_BACKEND_FAILURE", body="still found this")],
        dashboard=XoError("refused the pool dashboard"),
    )

    assert len(report.findings) == 1
    unread = [source.name for source in report.unread_sources]
    assert unread == ["dashboard"]
    read = [source.name for source in report.sources if source.read]
    assert "messages" in read and "tasks" in read


def test_every_source_is_recorded_whether_or_not_it_produced_anything() -> None:
    report = _run()

    names = [source.name for source in report.sources]
    assert names == [
        "messages",
        "alarms",
        "tasks",
        "patches",
        "backups",
        "restores",
        "dashboard",
    ]
    assert all(source.read for source in report.sources)


def test_findings_sort_worst_first_then_most_recent() -> None:
    report = _run(
        messages=[
            _message("POOL_MASTER_TRANSITION", ago_days=1, obj="p"),
            _message("SR_BACKEND_FAILURE", body="older", ago_days=5, obj="sr-a"),
            _message("SR_DISK_SPACE_LOW", body="newer", ago_days=2, obj="sr-b"),
            _message("VDI_CBT_METADATA_INCONSISTENT", body="cbt", ago_days=3, obj="vdi"),
        ]
    )

    severities = [finding.severity for finding in report.findings]
    assert severities == [CRITICAL, CRITICAL, WARNING, INFO]
    # Within critical, the more recent one leads.
    assert report.findings[0].evidence == "newer"


def test_counts_include_the_severities_that_found_nothing() -> None:
    """ "No critical findings" is the answer someone opens the page for."""
    report = _run(messages=[_message("POOL_MASTER_TRANSITION")])

    assert report.counts == {"critical": 0, "warning": 0, "info": 1}


def test_evidence_is_masked_with_the_redaction_rules() -> None:
    """A findings report is sent onward, so it is masked like a bundle."""
    report = _run(messages=[_message("SR_BACKEND_FAILURE", body="mount from 10.20.30.41 failed")])

    evidence = report.findings[0].evidence
    assert "10.20.30.41" not in evidence
    assert "failed" in evidence


def test_a_switched_off_rule_leaves_that_value_unmasked() -> None:
    """The other side of the test above: masking follows the stored settings.

    Without this, a test asserting only that an address disappears would pass
    just as well against a hard-coded mask that ignores the settings entirely.
    """
    from app.redact import RULES

    without_ipv4 = frozenset(rule.name for rule in RULES if rule.name != "ipv4")
    report = collect_findings(
        FakeXo(messages=[_message("SR_BACKEND_FAILURE", body="mount from 10.20.30.41 failed")]),
        [POOL],
        enabled=without_ipv4,
        now=NOW,
    )

    assert "10.20.30.41" in report.findings[0].evidence


def test_long_evidence_is_truncated_after_masking_not_before() -> None:
    """Cutting first could leave half a placeholder; cutting after cannot."""
    body = "10.20.30.41 " * 400
    report = _run(messages=[_message("SR_BACKEND_FAILURE", body=body)])

    evidence = report.findings[0].evidence
    assert len(evidence) <= 801
    assert "10.20.30.41" not in evidence


def test_no_pools_marks_the_patch_source_unread_rather_than_clean() -> None:
    report = collect_findings(FakeXo(), [], now=NOW)

    unread = {source.name for source in report.unread_sources}
    assert unread == {"patches"}


def test_progress_is_reported_at_every_source() -> None:
    """The job's cancellation checkpoint is its progress call.

    A source that never reports progress is one a findings run could not be
    interrupted at, so this asserts every source reports rather than that any
    particular percentage does.
    """
    seen: list[tuple[int, str]] = []
    collect_findings(FakeXo(), [POOL], now=NOW, progress=lambda p, s: seen.append((p, s)))

    assert len(seen) == 7
    assert [percent for percent, _ in seen] == sorted(percent for percent, _ in seen)


def test_log_findings_progress_is_reported_and_does_not_jump(tmp_path) -> None:
    """Mirrors test_progress_is_reported_at_every_source, for the log scan.

    A whole-tar pass reporting only 10 -> 90 -> 100 would make the bar jump
    rather than move, so this asserts it is called per member and never goes
    backwards, not that any particular percentage appears.
    """
    log_a = tmp_path / "a.log"
    log_a.write_text("multipathd reports failed path\n" * 20)
    log_b = tmp_path / "b.log"
    log_b.write_text("Storage SR reports I/O error\n" * 20)
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log_a, arcname="var/log/a.log")
        archive.add(log_b, arcname="var/log/b.log")

    seen: list[tuple[int, str]] = []
    collect_log_findings(bundle, progress=lambda p, s: seen.append((p, s)))

    assert len(seen) == 2
    assert [percent for percent, _ in seen] == sorted(percent for percent, _ in seen)
    assert all(10 <= percent <= 90 for percent, _ in seen)


def test_log_findings_reads_a_bundle_and_groups_repeated_lines(tmp_path) -> None:
    log = tmp_path / "xensource.log"
    log.write_text(
        "\n".join(
            [
                "multipathd reports failed path",
                "multipathd reports failed path",
                "HA fencing after heartbeat lost",
                "Storage SR reports I/O error",
                "XAPI internal error and backtrace",
            ]
        )
    )
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    report = collect_log_findings(bundle)

    assert {source.name for source in report.sources} == {
        "logs_storage",
        "logs_multipath",
        "logs_xapi",
        "logs_ha",
        "logs_oom",
        "logs_clockskew",
    }
    assert len(report.findings) == 4
    assert sorted(finding.count for finding in report.findings) == [1, 1, 1, 2]


def test_log_findings_date_range_excludes_lines_outside_the_window(tmp_path) -> None:
    from app.log_dates import DateRange

    log = tmp_path / "xensource.log"
    log.write_text(
        "\n".join(
            [
                "2026-01-15T00:00:00 multipathd reports failed path",
                "2026-03-01T00:00:00 HA fencing after heartbeat lost",
            ]
        )
    )
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    date_range = DateRange(
        start=_dt(2026, 1, 1).timestamp(), end=_dt(2026, 1, 31, 23, 59, 59).timestamp()
    )
    report = collect_log_findings(bundle, date_range=date_range)

    titles = [finding.title for finding in report.findings]
    assert any("Multipath" in title for title in titles)
    assert not any("High availability" in title for title in titles)


def test_log_findings_date_range_keeps_unparsable_lines(tmp_path) -> None:
    """A line with no recognisable timestamp is never excluded by a date
    range — see log_dates.line_in_range's best-effort rule."""
    from app.log_dates import DateRange

    log = tmp_path / "xensource.log"
    log.write_text("multipathd reports failed path with no timestamp at all")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    date_range = DateRange(
        start=_dt(2026, 1, 1).timestamp(), end=_dt(2026, 1, 31, 23, 59, 59).timestamp()
    )
    report = collect_log_findings(bundle, date_range=date_range)

    assert len(report.findings) == 1


def test_log_findings_no_date_range_means_no_filtering(tmp_path) -> None:
    log = tmp_path / "xensource.log"
    log.write_text("2026-03-01T00:00:00 multipathd reports failed path")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    report = collect_log_findings(bundle)

    assert len(report.findings) == 1


def test_log_findings_date_range_recorded_on_the_report(tmp_path) -> None:
    from app.log_dates import DateRange

    log = tmp_path / "xensource.log"
    log.write_text("nothing interesting")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    start, end = _dt(2026, 1, 1).timestamp(), _dt(2026, 1, 31).timestamp()
    report = collect_log_findings(bundle, date_range=DateRange(start=start, end=end))

    assert report.date_start == pytest.approx(start)
    assert report.date_end == pytest.approx(end)


def test_log_findings_redact_evidence(tmp_path) -> None:
    log = tmp_path / "xensource.log"
    log.write_text("Storage SR reports I/O error from 10.20.30.41")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    report = collect_log_findings(bundle)

    assert "10.20.30.41" not in report.findings[0].evidence
    # Tagged with its condition family at the point the rule fires — see
    # test_correlate_reports_confirms_findings_by_family_tag_alone for why
    # this matters beyond just this field having a value.
    assert report.findings[0].family == "storage"


def test_generic_xapi_error_is_not_reported_as_storage_failure(tmp_path) -> None:
    log = tmp_path / "xensource.log"
    log.write_text("xapi: Unexpected exception in message hook")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/xensource.log")

    report = collect_log_findings(bundle)

    assert [finding.source for finding in report.findings] == ["logs_xapi"]


def test_oom_killer_is_reported_and_a_quiet_log_is_not(tmp_path) -> None:
    log = tmp_path / "kern.log"
    log.write_text("Out of memory: Killed process 4821 (qemu-dm) total-vm:2048000kB")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/kern.log")

    report = collect_log_findings(bundle)

    assert [finding.source for finding in report.findings] == ["logs_oom"]

    quiet = tmp_path / "quiet.log"
    quiet.write_text("dom0 memory usage nominal")
    quiet_bundle = tmp_path / "quiet.tar.gz"
    with tarfile.open(quiet_bundle, "w:gz") as archive:
        archive.add(quiet, arcname="var/log/quiet.log")

    assert collect_log_findings(quiet_bundle).is_clean


def test_clock_skew_is_reported_and_a_healthy_sync_is_not(tmp_path) -> None:
    log = tmp_path / "ntp.log"
    log.write_text("chronyd: Can't synchronise: no majority")
    bundle = tmp_path / "logs.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(log, arcname="var/log/ntp.log")

    report = collect_log_findings(bundle)

    assert [finding.source for finding in report.findings] == ["logs_clockskew"]

    healthy = tmp_path / "healthy.log"
    healthy.write_text("chronyd: Selected source 10.0.0.1")
    healthy_bundle = tmp_path / "healthy.tar.gz"
    with tarfile.open(healthy_bundle, "w:gz") as archive:
        archive.add(healthy, arcname="var/log/healthy.log")

    assert collect_log_findings(healthy_bundle).is_clean


def test_a_truncated_bundle_still_yields_the_findings_read_before_the_break(
    tmp_path,
) -> None:
    """The case measured against a real pool: the download arrives with its
    last member and its terminator missing.

    What was read before the break is real, matching the salvage
    ``job_collect``'s redacted repack already does for the same truncated
    download — discarding it would lose an analysis of a bundle that takes
    minutes to collect again.
    """
    log_a = tmp_path / "a.log"
    log_a.write_text("Storage SR reports I/O error\n")
    # High-entropy padding, not repeated text: it must not compress away to
    # nothing, or the cut below lands in the gzip footer rather than inside
    # this member's own data, and the truncation would not be a mid-archive one.
    log_b = tmp_path / "b.log"
    log_b.write_text(os.urandom(300_000).hex())
    whole = tmp_path / "whole.tar.gz"
    with tarfile.open(whole, "w:gz") as archive:
        archive.add(log_a, arcname="var/log/a.log")
        archive.add(log_b, arcname="var/log/b.log")

    # Cut the terminator and the tail of the gzip stream off.
    whole_bytes = whole.read_bytes()
    bundle = tmp_path / "truncated.tar.gz"
    bundle.write_bytes(whole_bytes[: int(len(whole_bytes) * 0.75)])

    report = collect_log_findings(bundle)

    assert report.truncated is True
    assert any(finding.source == "logs_storage" for finding in report.findings)


def test_a_bundle_unreadable_from_the_first_byte_still_raises(tmp_path) -> None:
    """The other side: nothing salvageable is a different matter from a break
    partway through, and must still be reported as a failure."""
    bundle = tmp_path / "garbage.tar.gz"
    bundle.write_bytes(b"not a tar archive at all")

    with pytest.raises(tarfile.TarError):
        collect_log_findings(bundle)


def test_a_cancelling_progress_callback_stops_the_run() -> None:
    """Raising from progress is how a job is cancelled; it must travel."""

    class Cancelled(Exception):
        pass

    def cancel(percent: int, step: str) -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        collect_findings(FakeXo(), [POOL], now=NOW, progress=cancel)


def test_a_finding_with_an_unknown_severity_sorts_last_rather_than_raising() -> None:
    """Reading back a report written by a later version must not raise."""
    odd = Finding(severity="apocalyptic", title="?", evidence="", action="", source="messages")
    assert odd.severity_rank == 3


def test_date_range_start_bounds_what_is_asked_of_xen_orchestra() -> None:
    """An explicit range's start is used the same way ``window_days`` is —
    passed to Xen Orchestra as the lower bound of what it returns."""
    from app.log_dates import DateRange

    seen: list[float] = []

    class Recording(FakeXo):
        def messages(self, since: float) -> list[dict]:
            seen.append(since)
            return []

    start = NOW - 10 * 86400
    collect_findings(Recording(), [POOL], now=NOW, date_range=DateRange(start=start, end=None))

    assert seen == [pytest.approx(start)]


def test_date_range_end_excludes_events_after_it() -> None:
    """Xen Orchestra has no upper-bound filter of its own, so an event after
    the range's end is dropped here, against what the server already
    returned."""
    from app.log_dates import DateRange

    report = collect_findings(
        FakeXo(
            messages=[
                _message("SR_BACKEND_FAILURE", body="inside", ago_days=5, obj="sr-a"),
                _message("SR_BACKEND_FAILURE", body="too recent", ago_days=1, obj="sr-b"),
            ]
        ),
        [POOL],
        now=NOW,
        date_range=DateRange(start=NOW - 20 * 86400, end=NOW - 3 * 86400),
    )

    assert len(report.findings) == 1
    assert "sr-a" in report.findings[0].evidence or report.findings[0].object_id == "sr-a"


def test_date_range_overrides_window_days_entirely() -> None:
    """A caller giving both gets the range, not the window — the range is the
    more specific request."""
    from app.log_dates import DateRange

    report = collect_findings(
        FakeXo(messages=[_message("SR_BACKEND_FAILURE", body="old", ago_days=200, obj="sr-a")]),
        [POOL],
        now=NOW,
        window_days=1,
        date_range=DateRange(start=NOW - 365 * 86400, end=None),
    )

    assert len(report.findings) == 1


def test_date_range_is_recorded_on_the_report() -> None:
    from app.log_dates import DateRange

    start, end = NOW - 30 * 86400, NOW
    report = collect_findings(FakeXo(), [POOL], now=NOW, date_range=DateRange(start=start, end=end))

    assert report.date_start == pytest.approx(start)
    assert report.date_end == pytest.approx(end)


def test_no_date_range_leaves_report_fields_none() -> None:
    report = collect_findings(FakeXo(), [POOL], now=NOW, window_days=30)

    assert report.date_start is None
    assert report.date_end is None


def test_the_window_bounds_what_is_asked_of_xen_orchestra() -> None:
    """The read is bounded server-side, so the response shrinks with the window.

    Asserted on the ``since`` the engine passes down, because the alternative —
    fetching everything and filtering here — grows with pool age and was
    measured returning the oldest records rather than the newest.
    """
    seen: list[float] = []

    class Recording(FakeXo):
        def messages(self, since: float) -> list[dict]:
            seen.append(since)
            return []

    collect_findings(Recording(), [POOL], now=NOW, window_days=30)

    assert seen == [pytest.approx(NOW - 30 * 86400)]


def test_created_at_is_recorded_so_the_page_can_say_how_old_it_is() -> None:
    report = collect_findings(FakeXo(), [POOL])

    assert report.created_at == pytest.approx(time.time(), abs=5)


def test_the_report_records_which_redaction_rules_were_off() -> None:
    """An unmasked value and a value no rule looked for read identically.

    Measured on a real instance: with the UUID rule switched off from earlier
    testing, a finding's evidence carried a bare OpaqueRef. Nothing on the page
    or in the Markdown said masking had been partial, so a report about to be
    pasted into a support ticket looked fully masked.
    """
    from app.redact import RULES

    without = frozenset(rule.name for rule in RULES if rule.name not in ("uuid", "ipv4"))
    report = collect_findings(FakeXo(), [POOL], enabled=without, now=NOW)

    assert sorted(report.rules_disabled) == ["IPv4 addresses", "UUIDs"]


def test_a_report_with_every_rule_on_records_none_as_off() -> None:
    """The other side: the warning must not be stuck on for everybody."""
    from app.redact import RULES

    everything = frozenset(rule.name for rule in RULES)

    assert collect_findings(FakeXo(), [POOL], enabled=everything, now=NOW).rules_disabled == []
    # None means "all rules", which is what redact_line itself takes it to mean.
    assert collect_findings(FakeXo(), [POOL], enabled=None, now=NOW).rules_disabled == []


def test_each_source_says_whether_it_is_the_hosts_or_xen_orchestra() -> None:
    """XO serves all seven routes; it originates only three of them.

    A XAPI message means log in to the host; a failed task means look in Xen
    Orchestra. The table naming only the route cannot tell an operator which.
    """
    report = _run()
    origins = {source.name: source.origin for source in report.sources}

    assert origins == {
        "messages": "XCP-ng hosts",
        "alarms": "XCP-ng hosts",
        "tasks": "Xen Orchestra",
        "patches": "XCP-ng hosts",
        "backups": "Xen Orchestra",
        "restores": "Xen Orchestra",
        "dashboard": "XCP-ng hosts",
    }


def test_a_zero_count_says_what_was_checked_rather_than_just_zero() -> None:
    """ "0" cannot distinguish "none exist" from "nothing was looked at"."""
    report = _run()
    by_name = {source.name: source for source in report.sources}

    assert by_name["alarms"].examined_text == "checked - no alarms"


def test_the_patch_source_names_the_pools_it_checked() -> None:
    """ "0 patches" is the answer here, not an absence of data.

    Which pools were asked is what makes the zero trustworthy, so it is stated
    rather than left to be inferred from a count.
    """
    clean = _run(missing_patches=[])
    behind = _run(missing_patches=[{"name": "XS83E001"}])

    by_name = {source.name: source for source in clean.sources}
    assert by_name["patches"].examined_text == "none missing - Pool1 up to date"

    by_name = {source.name: source for source in behind.sources}
    assert by_name["patches"].examined_text == "1 missing on Pool1"


def test_a_nonzero_count_is_phrased_with_its_own_unit() -> None:
    report = _run(messages=[_message("VM_STARTED"), _message("VM_SHUTDOWN")])
    by_name = {source.name: source for source in report.sources}

    assert by_name["messages"].examined_text == "2 messages"


def test_an_unread_source_reports_no_count_at_all() -> None:
    report = _run(dashboard=XoError("refused"))
    by_name = {source.name: source for source in report.sources}

    assert by_name["dashboard"].examined_text == "-"


def test_a_source_name_this_version_does_not_know_still_renders() -> None:
    """A report written by a later version must load, not raise."""
    unknown = SourceResult("something_new", read=True, examined=5)

    assert unknown.title == "something_new"
    assert unknown.origin == ""
    assert unknown.examined_text == "5 records"


def test_correlate_reports_confirms_findings_in_the_same_family_and_time_window() -> None:
    """An HA fencing event seen by both the API and the logs is one incident.

    Both sides need to say so — the API report should not read as a lone XO
    event when the same fault left a trace on the host, and vice versa.
    """
    api_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="A host was fenced by high availability",
                evidence="HA_HOST_WAS_FENCED",
                action="Find why the host stopped responding.",
                source="messages",
                at=NOW,
            ),
        ],
    )
    log_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="High availability reported a fencing or heartbeat failure",
                evidence="HA fencing after heartbeat lost",
                action="Check host reachability.",
                source="logs_ha",
                at=NOW + 120,
            ),
        ],
    )

    correlate_reports(api_report, log_report)

    assert (
        api_report.findings[0].confirmed_by
        == "logs: High availability reported a fencing or heartbeat failure"
    )
    assert log_report.findings[0].confirmed_by == "API: A host was fenced by high availability"


def test_correlate_reports_does_not_confirm_across_families_or_outside_the_window() -> None:
    """Two unrelated problems, or the same family far apart in time, are not
    the same incident and must not be linked."""
    api_report = Report(
        findings=[
            Finding(
                severity=WARNING,
                title="A licence has expired",
                evidence="LICENSE_EXPIRED",
                action="Renew the licence.",
                source="messages",
                at=NOW,
            ),
        ],
    )
    log_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="The logs contain a storage failure",
                evidence="Storage SR reports I/O error",
                action="Check the storage repository.",
                source="logs_storage",
                at=NOW,
            ),
        ],
    )

    correlate_reports(api_report, log_report)

    assert api_report.findings[0].confirmed_by == ""
    assert log_report.findings[0].confirmed_by == ""

    # Same family, far outside the correlation window.
    far_api = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="A storage repository backend failed",
                evidence="SR_BACKEND_FAILURE",
                action="Check the SR.",
                source="messages",
                at=NOW,
            ),
        ],
    )
    far_log = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="The logs contain a storage failure",
                evidence="Storage SR reports I/O error",
                action="Check the storage repository.",
                source="logs_storage",
                at=NOW + 100_000,
            ),
        ],
    )

    correlate_reports(far_api, far_log)

    assert far_api.findings[0].confirmed_by == ""
    assert far_log.findings[0].confirmed_by == ""


def test_correlate_reports_uses_the_log_reports_created_at_when_a_finding_has_none() -> None:
    """A real log finding has no timestamp of its own — ``collect_log_findings``
    never sets ``Finding.at`` — so the window check must not be skipped just
    because it is missing. It falls back to when the bundle was scanned.

    Regression test: earlier, ``api_finding.at is not None and log_finding.at
    is not None`` skipped the window entirely whenever either side lacked a
    timestamp, so *any* two same-family findings matched regardless of how far
    apart in time they actually happened — which is every real log finding,
    since none of them ever carries one.
    """
    api_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="A storage repository backend failed",
                evidence="SR_BACKEND_FAILURE",
                action="Check the SR.",
                source="messages",
                at=NOW,
            ),
        ],
        created_at=NOW,
    )

    # Within the window of the API finding: still confirms.
    recent_log_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="The logs contain a storage failure",
                evidence="Storage SR reports I/O error",
                action="Check the storage repository.",
                source="logs_storage",
                at=None,
            ),
        ],
        created_at=NOW + 60,
    )
    correlate_reports(api_report, recent_log_report)
    assert api_report.findings[0].confirmed_by == "logs: The logs contain a storage failure"
    assert recent_log_report.findings[0].confirmed_by == (
        "API: A storage repository backend failed"
    )

    # A log bundle scanned weeks after the API finding: must not confirm just
    # because the log finding itself has no timestamp.
    stale_api = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="A storage repository backend failed",
                evidence="SR_BACKEND_FAILURE",
                action="Check the SR.",
                source="messages",
                at=NOW,
            ),
        ],
        created_at=NOW,
    )
    stale_log_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="The logs contain a storage failure",
                evidence="Storage SR reports I/O error",
                action="Check the storage repository.",
                source="logs_storage",
                at=None,
            ),
        ],
        created_at=NOW + 1_000_000,
    )
    correlate_reports(stale_api, stale_log_report)
    assert stale_api.findings[0].confirmed_by == ""
    assert stale_log_report.findings[0].confirmed_by == ""


def test_correlate_reports_confirms_findings_by_family_tag_alone() -> None:
    """Two findings whose titles and evidence share no keyword at all still
    correlate, because they were tagged with the same family by the rule that
    raised them — not by re-deriving the family from their text.

    Regression test: before ``Finding.family`` existed, ``_finding_family``
    only ever matched by scanning title+evidence for hand-picked keywords
    (``_CORRELATION_FAMILIES``). A rule whose wording happened not to contain
    one of those keywords could never correlate, regardless of how obviously
    related the two findings actually were to the rules that raised them. This
    reproduces that case: neither finding's title or evidence contains any
    word any `_CORRELATION_FAMILIES` pattern looks for.
    """
    api_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="Completely unrelated wording, no keywords here",
                evidence="Nothing here matches any regex either",
                action="Do something.",
                source="messages",
                at=NOW,
                family="storage",
            ),
        ],
    )
    log_report = Report(
        findings=[
            Finding(
                severity=CRITICAL,
                title="Also nothing a keyword scan would catch",
                evidence="Still nothing here to match on",
                action="Do something else.",
                source="logs_storage",
                at=NOW + 60,
                family="storage",
            ),
        ],
    )

    correlate_reports(api_report, log_report)

    assert api_report.findings[0].confirmed_by == "logs: Also nothing a keyword scan would catch"
    assert log_report.findings[0].confirmed_by == (
        "API: Completely unrelated wording, no keywords here"
    )


def test_correlate_reports_handles_a_missing_report() -> None:
    """Only one report exists yet — nothing to correlate against, not an error."""
    finding = Finding(severity=INFO, title="x", evidence="", action="", source="messages")
    report = Report(findings=[finding])

    correlate_reports(report, None)
    correlate_reports(None, report)

    assert report.findings[0].confirmed_by == ""
