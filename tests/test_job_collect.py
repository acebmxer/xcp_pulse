"""The Collect logs job: what it downloads, what it masks, and what it keeps.

The thing that has to be right here is the redacted bundle. It is the file an
operator sends to Vates, so a member that survives the repack unmasked is a
credential leak with a green tick beside it. Most of what follows opens the
produced tarball and reads what is actually inside it, rather than trusting the
report's counts.

The second thing is that the raw copy is kept and is *not* masked: the redacted
copy is lossy, and a question about what was masked can only be answered
against the original.

Downloads are faked at the client, not over HTTP: what is under test is the
job's use of the client, and a real transfer would make this a network test.
"""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.artifacts import artifact_path, list_for_job
from app.db import init_db
from app.job_collect import KIND, build_report
from app.job_redact import REPORT_ARTIFACT, report_from_job
from app.job_runner import JobWorker
from app.jobs import FAILED, SUCCEEDED, enqueue, get_job
from app.redact import redact_text, set_enabled_rules
from app.xo_client import XoError
from app.xo_connection import save_connection

SECRET = "test-secret-key-not-for-production"

HOST_ID = "host-1"
HOST_NAME = "xcp-ng-host1"

# One member of a real bundle, in miniature: an address, a UUID, a MAC and a
# session token, which between them exercise four different rules.
XENSOURCE_LOG = """\
Sep  6 12:30:45 xen01 xapi: trackid=a3f9c2b18e4d0c67 user=admin
Sep  6 12:30:46 xen01 xapi: host 4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34 at 10.20.30.41
Sep  6 12:30:47 xen01 xenopsd: VIF 3a:4b:5c:6d:7e:8f on 10.20.30.55
"""

SMLOG = "Sep  6 12:31:02 xen01 SM: mount 10.20.30.9 password=Str0ngPass!\n"

AUDIT = "Sep  6 12:32:00 xapi audit: session from 10.20.30.77 uid=root\n"


class _Settings:
    secret_key = SECRET


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "test.db")
    save_connection(
        conn,
        url="https://xo.example.com",
        token="stored-token",
        account_type="admin",
        verify_tls=True,
        secret_key=SECRET,
    )
    return conn


@pytest.fixture
def worker(tmp_path: Path) -> JobWorker:
    return JobWorker(tmp_path / "test.db", tmp_path, _Settings())


def _bundle_bytes(members: dict[str, str]) -> bytes:
    """A ``.tgz`` holding the named text members, as XO would serve one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in members.items():
            body = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _fake_client(bundle: bytes = b"", audit: str = AUDIT):
    """A stand-in XoClient whose downloads write the given bodies.

    Mirrors the real signature — including ``on_chunk`` — because the job's
    progress and cancellation both run through that callback, and a fake that
    never called it would let a broken one pass.
    """
    payload = bundle or _bundle_bytes({"var/log/xensource.log": XENSOURCE_LOG})

    class _Client:
        # Counted rather than merely recorded: the point of the audit checkbox
        # is that an unticked box skips a 770 MiB transfer, which an assertion
        # about stored artifacts alone would not prove.
        audit_calls = 0

        def download_logs(self, host_id, destination, *, on_chunk=None):
            destination.write_bytes(payload)
            if on_chunk is not None:
                on_chunk(len(payload), len(payload))
            return len(payload)

        def download_audit(self, host_id, destination, *, on_chunk=None):
            self.__class__.audit_calls += 1
            body = audit.encode("utf-8")
            destination.write_bytes(body)
            if on_chunk is not None:
                on_chunk(len(body), len(body))
            return len(body)

    return _Client()


# The audit trail is off by default in the app, so the tests that exercise it
# have to ask for it the same way the page's checkbox does.
_DEFAULT_PARAMS = {"host_id": HOST_ID, "host_name": HOST_NAME, "include_audit": True}
_NO_AUDIT_PARAMS = {"host_id": HOST_ID, "host_name": HOST_NAME}


def _run(conn, worker, client=None, params=_DEFAULT_PARAMS) -> str:
    # Default as a sentinel rather than ``params or ...``, so a test passing an
    # empty dict deliberately gets an empty dict rather than the default.
    job = enqueue(conn, KIND, params)
    with patch("app.job_collect.build_client", return_value=client or _fake_client()):
        worker.run_one(conn)
    return job.id


def _named(conn, job_id: str) -> dict[str, object]:
    return {item.name: item for item in list_for_job(conn, job_id)}


def _members(path: Path) -> dict[str, str]:
    """Every text member of a stored tarball, by name."""
    out: dict[str, str] = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            body = archive.extractfile(member)
            out[member.name] = body.read().decode("utf-8") if body else ""
    return out


def test_a_collection_stores_raw_and_redacted_copies_of_both_downloads(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker)

    assert get_job(conn, job_id).state == SUCCEEDED
    assert set(_named(conn, job_id)) == {
        f"{HOST_NAME}-logs.tgz",
        f"{HOST_NAME}-logs.redacted.tgz",
        f"{HOST_NAME}-audit.txt",
        f"{HOST_NAME}-audit.redacted.txt",
        REPORT_ARTIFACT,
    }


def test_without_the_audit_flag_the_trail_is_neither_fetched_nor_stored(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """The default collection is the bundle alone.

    Both sides of this condition need a test: one that only checked the flag
    switched on would pass just as well against a flag that is always on, which
    is the whole thing being fixed here.
    """
    client = _fake_client()
    job_id = _run(conn, worker, client=client, params=_NO_AUDIT_PARAMS)

    assert get_job(conn, job_id).state == SUCCEEDED
    assert set(_named(conn, job_id)) == {
        f"{HOST_NAME}-logs.tgz",
        f"{HOST_NAME}-logs.redacted.tgz",
        REPORT_ARTIFACT,
    }
    # Not merely unstored — the 770 MiB download must not happen at all.
    assert client.audit_calls == 0


def test_the_report_covers_only_the_files_a_collection_produced(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker, params=_NO_AUDIT_PARAMS)
    report = report_from_job(conn, tmp_path, job_id)

    names = [(pair["raw"]["name"], pair["redacted"]["name"]) for pair in report["files"]]
    assert names == [(f"{HOST_NAME}-logs.tgz", f"{HOST_NAME}-logs.redacted.tgz")]


def test_the_redacted_bundle_holds_no_unmasked_values(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The claim the download button makes, checked against the file itself."""
    job_id = _run(
        conn,
        worker,
        _fake_client(
            _bundle_bytes({"var/log/xensource.log": XENSOURCE_LOG, "var/log/SMlog": SMLOG})
        ),
    )
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    text = "".join(_members(redacted).values())
    for secret in (
        "10.20.30.41",
        "10.20.30.55",
        "10.20.30.9",
        "4c8a1f2e-77b3-4a91-9f0e-2d5c8b1a6e34",
        "3a:4b:5c:6d:7e:8f",
        "a3f9c2b18e4d0c67",
        "Str0ngPass!",
    ):
        assert secret not in text, f"{secret} survived the repack"


def test_the_repack_masks_exactly_what_the_preview_would(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Byte for byte, per member. A divergence here makes the preview a lie."""
    job_id = _run(conn, worker)
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    expected, _ = redact_text(XENSOURCE_LOG)
    assert _members(redacted)["var/log/xensource.log"] == expected


def test_the_raw_bundle_is_kept_unmasked(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The original is the only thing that can answer "what was masked?"."""
    job_id = _run(conn, worker)
    stored = _named(conn, job_id)
    raw = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.tgz"].id)

    assert _members(raw)["var/log/xensource.log"] == XENSOURCE_LOG


def test_the_redacted_bundle_keeps_the_archive_layout(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Names and order survive, so the copy opens like the original."""
    members = {
        "var/log/xensource.log": XENSOURCE_LOG,
        "var/log/SMlog": SMLOG,
        "var/log/daemon.log": "nothing sensitive here\n",
    }
    job_id = _run(conn, worker, _fake_client(_bundle_bytes(members)))
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    assert list(_members(redacted)) == list(members)


def test_a_member_whose_masked_body_changes_length_is_still_readable(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The member header's size has to be corrected, or the archive truncates.

    The member after the masked one is what proves it: a stale size leaves the
    reader at the wrong offset and it never appears.
    """
    members = {
        "a.log": "addr 10.20.30.41 and 10.20.30.42 and 10.20.30.43\n",
        "b.log": "the member after the one that changed length\n",
    }
    job_id = _run(conn, worker, _fake_client(_bundle_bytes(members)))
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    read_back = _members(redacted)
    assert read_back["b.log"] == members["b.log"]
    assert "10.20.30.41" not in read_back["a.log"]


def test_a_compressed_member_is_copied_through_untouched(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Masking gzip bytes would rewrite the file and mask nothing."""
    buffer = io.BytesIO()
    compressed = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03not really gzip"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("var/log/xensource.log.1.gz")
        info.size = len(compressed)
        archive.addfile(info, io.BytesIO(compressed))

    job_id = _run(conn, worker, _fake_client(buffer.getvalue()))
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    with tarfile.open(redacted, "r:*") as archive:
        body = archive.extractfile("var/log/xensource.log.1.gz").read()
    assert body == compressed


def test_the_audit_trail_is_redacted_too(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    job_id = _run(conn, worker)
    stored = _named(conn, job_id)
    path = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-audit.redacted.txt"].id)

    expected, _ = redact_text(AUDIT)
    assert path.read_text(encoding="utf-8") == expected


def test_the_report_counts_every_rule_across_both_files(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The counts have to span the whole run, not one file of it."""
    job_id = _run(conn, worker)
    report = report_from_job(conn, tmp_path, job_id)

    # Two addresses in the log member, one in the audit trail.
    by_name = {row["name"]: row for row in report["rules"]}
    assert by_name["ipv4"]["hits"] == 3
    assert report["host"] == {"id": HOST_ID, "name": HOST_NAME}


def test_the_report_names_the_raw_and_redacted_file_of_each_pair(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The counts stay attached to the files they describe."""
    job_id = _run(conn, worker)
    report = report_from_job(conn, tmp_path, job_id)

    names = [(pair["raw"]["name"], pair["redacted"]["name"]) for pair in report["files"]]
    assert names == [
        (f"{HOST_NAME}-logs.tgz", f"{HOST_NAME}-logs.redacted.tgz"),
        (f"{HOST_NAME}-audit.txt", f"{HOST_NAME}-audit.redacted.txt"),
    ]
    assert all(pair["raw"]["sha256"] for pair in report["files"])


def test_a_switched_off_rule_leaves_its_values_in_the_bundle(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """A collection uses the same settings the redaction page shows.

    This is the failure that matters most: a bundle collected with masking off
    reads as redacted unless the report says otherwise.
    """
    keep = {rule for rule in ("uuid", "mac", "secret", "trackid", "email", "hostname", "ipv6")}
    set_enabled_rules(conn, keep)

    job_id = _run(conn, worker)
    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    assert "10.20.30.41" in "".join(_members(redacted).values())

    report = report_from_job(conn, tmp_path, job_id)
    assert "ipv4" in report["rules_disabled"]
    assert next(row for row in report["rules"] if row["name"] == "ipv4")["enabled"] is False


def test_a_refused_download_fails_the_job_with_the_privilege_named(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """A restricted account is the expected failure, so it must read clearly."""

    class _Refusing:
        def download_logs(self, host_id, destination, *, on_chunk=None):
            raise XoError("Xen Orchestra refused /hosts/x/logs.tgz. Downloading logs needs …")

        def download_audit(self, host_id, destination, *, on_chunk=None):
            raise AssertionError("should not be reached")

    job_id = _run(conn, worker, _Refusing())

    job = get_job(conn, job_id)
    assert job.state == FAILED
    assert "refused" in job.error


def test_a_job_with_no_host_fails_rather_than_collecting_something_else(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    job_id = _run(conn, worker, params={})

    job = get_job(conn, job_id)
    assert job.state == FAILED
    assert "No host" in job.error


def test_a_bundle_that_is_not_a_tarball_fails_but_keeps_the_raw_download(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """The 100-second download is not thrown away because the repack failed."""
    job_id = _run(conn, worker, _fake_client(b"this is not a tar archive at all"))

    job = get_job(conn, job_id)
    assert job.state == FAILED
    assert "could not be read as an archive" in job.error
    assert f"{HOST_NAME}-logs.tgz" in _named(conn, job_id)


def test_a_host_name_with_path_characters_cannot_shape_a_filename(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """The name comes from XO, so it is operator-supplied text."""
    job_id = _run(
        conn,
        worker,
        params={"host_id": HOST_ID, "host_name": "../../etc/pool one"},
    )

    for name in _named(conn, job_id):
        assert "/" not in name
        assert ".." not in name


def test_the_finished_step_says_what_was_masked_and_stored(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """The job's own summary, which is what the collect page shows."""
    job_id = _run(conn, worker)

    step = get_job(conn, job_id).step
    assert HOST_NAME in step
    assert "masked" in step


def test_build_report_carries_every_rule_including_ones_that_matched_nothing() -> None:
    """Read directly, because "no hits" and "not looked for" must differ."""
    from app.artifacts import Artifact

    def _artifact(name: str) -> Artifact:
        return Artifact(
            id="a" * 32,
            job_id="b" * 32,
            name=name,
            media_type="application/gzip",
            size_bytes=10,
            sha256="c" * 64,
            created_at=0.0,
        )

    report = build_report(
        host_id=HOST_ID,
        host_name=HOST_NAME,
        enabled=frozenset({"ipv4"}),
        counts={"ipv4": 2},
        raw=[_artifact("x-logs.tgz")],
        redacted=[_artifact("x-logs.redacted.tgz")],
    )

    by_name = {row["name"]: row for row in report["rules"]}
    assert by_name["ipv4"] == {
        "name": "ipv4",
        "title": by_name["ipv4"]["title"],
        "placeholder": by_name["ipv4"]["placeholder"],
        "enabled": True,
        "hits": 2,
    }
    assert by_name["uuid"]["enabled"] is False
    assert by_name["uuid"]["hits"] == 0
    assert report["total_hits"] == 2


def test_a_cancelled_collection_stops_and_keeps_nothing_half_written(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Cancellation travels through the progress callback and unwinds the job.

    A 100-second download is the case cancelling exists for, so what matters is
    that asking mid-transfer stops it and leaves no partial file to be mistaken
    for a collection.
    """
    from app.artifacts import artifacts_dir
    from app.jobs import CANCELLED, request_cancel

    class _Slow:
        def download_logs(self, host_id, destination, *, on_chunk=None):
            destination.write_bytes(b"partial")
            # The operator presses Cancel; the next progress report raises.
            request_cancel(conn, job.id)
            try:
                on_chunk(7, 1000)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            raise AssertionError("the cancellation should have unwound this")

        def download_audit(self, host_id, destination, *, on_chunk=None):
            raise AssertionError("should not be reached after cancelling")

    job = enqueue(conn, KIND, _DEFAULT_PARAMS)
    with patch("app.job_collect.build_client", return_value=_Slow()):
        worker.run_one(conn)

    assert get_job(conn, job.id).state == CANCELLED
    job_dir = artifacts_dir(tmp_path) / job.id
    assert not any(job_dir.iterdir()) if job_dir.is_dir() else True


def test_a_truncated_bundle_still_yields_a_redacted_copy_of_what_arrived(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """The case measured against a real pool.

    The archive arrives with its last member and its terminator missing. What
    was read before the break is real, masked log data, and a collection takes
    minutes to repeat — so it is kept rather than discarded, and the job says
    the bundle ends early.
    """
    good = _bundle_bytes(
        {
            "var/log/xensource.log": XENSOURCE_LOG,
            "var/log/SMlog": SMLOG,
            "var/log/daemon.log": "nothing sensitive\n" * 200,
        }
    )
    # Cut the terminator and the tail of the gzip stream off.
    job_id = _run(conn, worker, _fake_client(good[: int(len(good) * 0.75)]))

    job = get_job(conn, job_id)
    assert job.state == SUCCEEDED, f"a truncated bundle must not lose the run: {job.error}"

    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    recovered = _members(redacted)
    assert recovered, "what was readable must be kept"
    assert "10.20.30.41" not in "".join(recovered.values())

    # The salvaged copy must be a well-formed archive, not merely readable.
    # Reported from a real run: without an end-of-archive marker, gzip reads
    # the file happily while every archive tool calls it corrupt and offers
    # only to open it read-only.
    with tarfile.open(redacted, "r:*") as archive:
        assert archive.getmembers(), "the redacted copy must open by random access"


def test_a_bundle_with_nothing_readable_still_fails(
    conn: sqlite3.Connection, worker: JobWorker
) -> None:
    """An empty redacted bundle beside a raw one invites sending the wrong file."""
    job_id = _run(conn, worker, _fake_client(b"\x1f\x8b" + b"\x00" * 500))

    job = get_job(conn, job_id)
    assert job.state == FAILED
    assert "could not be read as an archive" in job.error
    assert f"{HOST_NAME}-logs.tgz" in _named(conn, job_id), "the raw download is kept"


def test_a_bundle_cut_mid_member_still_opens_by_random_access(
    conn: sqlite3.Connection, worker: JobWorker, tmp_path: Path
) -> None:
    """Reported from a real run: the redacted copy opened only read-only.

    Ark called it corrupt. The cause was a member whose header promised more
    bytes than followed — ``addfile`` commits the header and then streams, so a
    source that dies part-way through a member leaves that header lying. An
    incomplete member is dropped instead, which is the same thing the source
    lost, and the rest of the archive stays usable.
    """
    members = {
        "var/log/xensource.log": XENSOURCE_LOG,
        "var/log/SMlog": SMLOG,
        # Large enough that cutting the stream lands inside it.
        "var/log/daemon.log": "addr 10.20.30.41 padding line\n" * 4000,
    }
    good = _bundle_bytes(members)
    job_id = _run(conn, worker, _fake_client(good[: int(len(good) * 0.6)]))

    job = get_job(conn, job_id)
    assert job.state == SUCCEEDED, f"salvage must keep the run: {job.error}"

    stored = _named(conn, job_id)
    redacted = artifact_path(tmp_path, job_id, stored[f"{HOST_NAME}-logs.redacted.tgz"].id)

    # Random access is what an archive tool uses; a lying header fails here
    # while a streaming read of the same file appears to work.
    with tarfile.open(redacted, "r:*") as archive:
        names = archive.getnames()
    assert names, "at least one whole member must survive"

    # Every member that made it must be complete, or the copy is corrupt again.
    with tarfile.open(redacted, "r:*") as archive:
        for member in archive.getmembers():
            if member.isfile():
                body = archive.extractfile(member)
                assert body is not None
                assert len(body.read()) == member.size, f"{member.name} is short"
