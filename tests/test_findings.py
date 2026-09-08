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

import time

import pytest

from app.findings import (
    CRITICAL,
    INFO,
    WARNING,
    Finding,
    collect_findings,
)
from app.xo_client import Pool, XoError

NOW = 1788700000.0
POOL = Pool(id="pool-1", name="Pool1")


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
    from app.findings import SourceResult

    unknown = SourceResult("something_new", read=True, examined=5)

    assert unknown.title == "something_new"
    assert unknown.origin == ""
    assert unknown.examined_text == "5 records"
