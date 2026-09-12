"""The "Collect logs" job: download a host's bundle and redact it for sending.

This is the job the rest of the system was built for. Everything it needs was
proven first on jobs that finish in milliseconds: the queue, progress,
cancellation, the artifact store, and the line-by-line redaction loop.

What it produces, per host:

* ``<host>-logs.tgz`` — the raw bundle exactly as Xen Orchestra served it,
  kept because the redacted copy is lossy and a question about what was masked
  can only be answered against the original;
* ``<host>-logs.redacted.tgz`` — the copy to send, every text member masked;
* ``<host>-audit.txt`` and its redacted copy — the XAPI audit trail, only when
  the collection asked for it;
* ``redaction-report.json`` — what was masked across the whole run.

**Redacting immediately is a choice, not a given.** ``params['redact']``
governs it, absent meaning on so a job queued before the checkbox existed
still gets the behaviour it was queued expecting. Switched off, this job
stores only the raw file(s) and no report — an operator who wants a different
rule set later runs the existing "Redact a stored file" job against the raw
bundle from the Jobs page, in seconds, without a second download.

**The audit trail is off unless asked for.** ``xen-bugtool`` already collects
``/var/log/audit.log`` and its rotated copies into the bundle above, so the
separate ``audit.txt`` route duplicates them — and at a measured 770 MiB it is
the largest file in a collection, larger than the bundle itself. What it adds
over the bundle is the current, uncompressed trail. Vates ask for a bugtool
status report, not this, so a collection produces it only when the operator
ticks the box.

**The raw bundle never leaves this machine by default.** The download button
offers the redacted copy; the raw one is downloadable too, because an operator
diagnosing their own pool needs it, but the page says which is which.

Expect about 433 MB and roughly two minutes per host, measured on XCP-ng 8.3.
The 100-second figure quoted elsewhere is the ``logs.tgz`` download alone; a
run also redacts a copy of it. With the audit trail included the run stores
about 2.3 GiB and measured runs took 166 and 203 seconds.
"""

from __future__ import annotations

import copy
import gzip
import tarfile
import time
import zlib
from typing import Any

from app.artifacts import Artifact, artifact_path, human_bytes, store_file, store_json
from app.job_redact import REPORT_ARTIFACT, redacted_name
from app.job_runner import register
from app.jobs import JobContext, get_job
from app.redact import active_rules, enabled_rules
from app.xo_client import XoClient
from app.xo_connection import build_client

KIND = "collect_logs"

TGZ_MEDIA_TYPE = "application/gzip"
TEXT_MEDIA_TYPE = "text/plain"

# What each artifact is called. The host's name is prefixed so a data volume
# holding several collections says which host each file came from without the
# operator having to open a job to find out.
LOGS_SUFFIX = "logs.tgz"
AUDIT_SUFFIX = "audit.txt"

# Members larger than this are copied into the redacted bundle without being
# read as text. A log bundle is text, but a member that is not — a core dump,
# an embedded archive — would otherwise be decoded line by line to no purpose.
# Nothing in a measured bundle exceeds this.
_MAX_REDACTABLE_MEMBER_BYTES = 256 * 1024 * 1024

# Members whose contents are not log text. Redacting a compressed member would
# mask nothing (the bytes are not the text) while rewriting the file, so they
# are copied through unchanged and the report says so.
_BINARY_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zip", ".tar", ".core")

# Progress bands. The download is the long part by a wide margin — 100 seconds
# against a few for the repack — so it owns most of the bar.
_DOWNLOAD_FROM, _DOWNLOAD_TO = 5, 60
_AUDIT_FROM, _AUDIT_TO = 60, 68
_REDACT_FROM, _REDACT_TO = 68, 92

# How often the repack reports progress. Every member would be a database write
# per file for a bar that moves in whole percent.
_PROGRESS_EVERY_MEMBERS = 25

# How often a download reports progress. Every chunk carries a changing step
# string, which defeats JobContext's unchanged-value guard and makes each one a
# write plus a cancellation read. Four times a second keeps the bar and the ETA
# moving while a 433 MB transfer stays a few hundred writes rather than a
# thousand.
_PROGRESS_EVERY_SECONDS = 0.25


def run(context: JobContext) -> None:
    """Collect one host's logs, storing the raw and redacted copies.

    The host arrives in ``params['host_id']``, with ``params['host_name']`` for
    naming the files. Raises rather than catching: the runner records the
    message against the job, which is where the operator looks for it.
    """
    job = get_job(context.conn, context.job_id)
    params = job.params if job else {}
    host_id = params.get("host_id")
    if not isinstance(host_id, str) or not host_id:
        raise ValueError("No host was named to collect from.")
    host_name = _safe_name(str(params.get("host_name") or host_id))
    # Absent means off: collections queued before the checkbox existed, and any
    # caller that omits the flag, get the smaller run rather than the 770 MiB
    # download that duplicates what is already inside the bundle.
    include_audit = bool(params.get("include_audit"))
    # Absent means on: a collection queued before this checkbox existed, or by
    # any caller that omits the flag, keeps redacting immediately rather than
    # silently starting to leave bundles unmasked.
    redact = bool(params.get("redact", True))

    context.progress(2, "Connecting to Xen Orchestra")
    client = build_client(context.conn, context.settings.secret_key)

    enabled = enabled_rules(context.conn)
    counts: dict[str, int] = {}
    produced: list[Artifact] = []

    raw_logs = _download(
        context,
        client,
        host_id=host_id,
        name=f"{host_name}-{LOGS_SUFFIX}",
        media_type=TGZ_MEDIA_TYPE,
        fetch=client.download_logs,
        band=(_DOWNLOAD_FROM, _DOWNLOAD_TO),
        step="Downloading the log bundle",
    )
    produced.append(raw_logs)

    raw_audit = None
    if include_audit:
        raw_audit = _download(
            context,
            client,
            host_id=host_id,
            name=f"{host_name}-{AUDIT_SUFFIX}",
            media_type=TEXT_MEDIA_TYPE,
            fetch=client.download_audit,
            band=(_AUDIT_FROM, _AUDIT_TO),
            step="Downloading the audit trail",
        )
        produced.append(raw_audit)

    if redact:
        context.progress(_REDACT_FROM, "Redacting the bundle")
        redacted_logs = _redact_tarball(context, raw_logs, enabled, counts)
        produced.append(redacted_logs)

        redacted_audit = None
        if raw_audit is not None:
            context.progress(_REDACT_TO, "Redacting the audit trail")
            redacted_audit = _redact_text_artifact(context, raw_audit, enabled, counts)
            produced.append(redacted_audit)

        context.progress(95, "Writing the report")
        report = build_report(
            host_id=host_id,
            host_name=host_name,
            enabled=enabled,
            counts=counts,
            raw=[item for item in (raw_logs, raw_audit) if item is not None],
            redacted=[item for item in (redacted_logs, redacted_audit) if item is not None],
        )
        store_json(
            context.conn,
            context.data_dir,
            job_id=context.job_id,
            name=REPORT_ARTIFACT,
            payload=report,
        )

        total_bytes = sum(item.size_bytes for item in produced)
        masked = report["total_hits"]
        context.progress(
            100,
            f"{host_name}: {masked} value(s) masked, {human_bytes(total_bytes)} stored",
        )
    else:
        # No report: there is nothing to report on. `report_from_job` already
        # treats a missing report as "not yet redacted" rather than an error,
        # which is exactly what this collection is until someone runs the
        # existing "Redact a stored file" job against the raw bundle above.
        total_bytes = sum(item.size_bytes for item in produced)
        context.progress(
            100,
            f"{host_name}: {human_bytes(total_bytes)} stored, unredacted",
        )


def _download(
    context: JobContext,
    client: XoClient,
    *,
    host_id: str,
    name: str,
    media_type: str,
    fetch,
    band: tuple[int, int],
    step: str,
) -> Artifact:
    """Stream one route to a temporary file and take it into the store.

    The file is written under this job's own artifact directory rather than a
    system temporary directory: a 433 MB download must land on the data volume,
    which is the disk sized for it, and ``store_file`` then moves it into place
    without a second copy.
    """
    low, high = band
    working = artifact_path(context.data_dir, context.job_id, f"{name}.part")
    working.parent.mkdir(parents=True, exist_ok=True)

    context.progress(low, step)
    started = time.time()
    # Mutable so the closure can update it; a plain float would be rebound to a
    # local inside on_chunk instead.
    last_report = [started]

    def on_chunk(written: int, total: int | None) -> None:
        # Cancellation is checked on *every* chunk, not only on the throttled
        # ones: a job whose cancel is noticed only when the progress line
        # happens to be due would ignore the button for a quarter of a second
        # at best, and for a whole slow chunk at worst.
        context.check_cancelled()

        # The progress write is throttled, because each one carries a changing
        # step string and so cannot be skipped by JobContext's unchanged-value
        # guard.
        now = time.time()
        if now - last_report[0] < _PROGRESS_EVERY_SECONDS:
            return
        last_report[0] = now
        context.progress(
            low if total is None else low + int(min(1.0, written / total) * (high - low)),
            _transfer_step(step, written, total, started),
        )

    fetch(host_id, working, on_chunk=on_chunk)

    context.progress(high, f"Storing {name}")
    return store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=name,
        media_type=media_type,
        source=working,
    )


def _transfer_step(step: str, written: int, total: int | None, started: float) -> str:
    """The progress line during a download, with an ETA once one is meaningful.

    An estimate from the first fraction of a second is noise, so none is shown
    until enough has transferred for the rate to mean something. Where XO sends
    no Content-Length there is no total to estimate against, and the line says
    how much has arrived rather than inventing a percentage.
    """
    if total is None:
        return f"{step} — {human_bytes(written)} so far"

    elapsed = time.time() - started
    line = f"{step} — {human_bytes(written)} of {human_bytes(total)}"
    if elapsed < 2.0 or written <= 0:
        return line
    remaining = (total - written) / (written / elapsed)
    if remaining < 1:
        return line
    return f"{line}, about {_duration(remaining)} left"


def _redact_tarball(
    context: JobContext,
    source: Artifact,
    enabled,
    counts: dict[str, int],
    *,
    member_filter=None,
    working_name: str = "repacking.tmp",
    store_name: str | None = None,
    progress_band: tuple[int, int] | None = None,
    progress_step: str = "Redacting the bundle",
) -> Artifact:
    """Repack a ``.tgz`` with every text member masked. Returns the new artifact.

    A tarball cannot be masked in place: each member is its own stream inside a
    gzip stream, so the copy is written member by member. Text members are read
    a line at a time — the same ``active_rules`` and ``Rule.apply`` the preview
    page and ``job_redact`` use, in the same order, so the three cannot mask
    differently. Do not add a fourth loop over the rules.

    Member metadata is copied across unchanged, so the redacted bundle has the
    same layout, names and timestamps as the original and is still readable by
    anything that reads the original.

    ``member_filter(member)``, when given, decides whether a member is kept at
    all — a directory entry is still offered so a filter matching by path can
    see it, but is never written unless it passes. This is what lets
    ``job_extract`` reuse this loop verbatim for "only these categories"
    instead of a second copy of the streaming/salvage/masking logic: a filtered
    extraction and a full redacted copy are the same operation with a different
    answer to "does this member belong in the output?".
    """
    source_path = artifact_path(context.data_dir, source.job_id, source.id)
    if not source_path.is_file():
        raise ValueError(f"The body of {source.name} is missing from the data volume.")

    working = artifact_path(context.data_dir, context.job_id, working_name)
    working.parent.mkdir(parents=True, exist_ok=True)
    rules = active_rules(enabled)
    members = 0
    kept = 0
    band = progress_band or (_REDACT_FROM, _REDACT_TO)
    band_from, band_to = band

    # Streaming mode ("r|gz") rather than random access, because a bundle whose
    # end is missing cannot be indexed — and a truncated bundle is the case that
    # matters: measured against one pool, the archive arrives with its last
    # member and its terminator absent. Streaming reads every member that is
    # intact and then stops, which is what makes salvage possible below.
    truncated = False
    # The output archive is closed in its own `finally` rather than by a `with`
    # around the read loop. A `with` unwinds on the read error a truncated
    # source raises, and tarfile then never writes its end-of-archive marker —
    # producing a copy that gzip reads happily but that every archive tool
    # reports as corrupt, and that must be opened read-only to see inside.
    # Closing it deliberately means the redacted copy is always a well-formed
    # archive of whatever was recovered.
    out = tarfile.open(working, "w:gz")
    try:
        with tarfile.open(source_path, "r|*") as archive:
            for member in archive:
                members += 1
                if members % _PROGRESS_EVERY_MEMBERS == 0:
                    context.progress(
                        band_from,
                        f"{progress_step} — {members} file(s)",
                    )

                if member_filter is not None and not member_filter(member):
                    continue
                kept += 1

                if not member.isfile():
                    # Directories, symlinks and device nodes have no body to
                    # mask; their metadata is copied so the layout survives.
                    out.addfile(member)
                    continue

                body = archive.extractfile(member)
                if body is None:
                    out.addfile(member)
                    continue

                if _is_binary(member):
                    # Read the body before writing anything. ``addfile`` commits
                    # the header and then streams, so a source that dies
                    # part-way through a member leaves a header promising more
                    # bytes than follow — and that member, not a missing
                    # terminator, is what makes the salvaged copy unreadable by
                    # random access: archive tools report it as corrupt and will
                    # only open it read-only. An incomplete member is dropped
                    # instead, which is the same thing the source lost.
                    payload = body.read()
                    if len(payload) < member.size:
                        raise _IncompleteMember(member.name)
                    out.addfile(member, _BytesReader(payload))
                    continue

                masked, complete = _mask_stream(body, rules, counts, member.size)
                if not complete:
                    raise _IncompleteMember(member.name)
                # The masked body is a different length — a placeholder rarely
                # matches what it replaced — so the header's size has to be
                # corrected or every member after this one is read at the wrong
                # offset and the archive truncates. Copied rather than mutated
                # so the member being iterated is left as the reader has it.
                header = copy.copy(member)
                header.size = len(masked)
                out.addfile(header, _BytesReader(masked))
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error, _IncompleteMember) as exc:
        # A truncated archive is not a lost collection. Everything read before
        # the break is real, masked log data, and discarding it would throw
        # away a download that takes minutes to repeat — so what was recovered
        # is kept and the job says how much.
        #
        # Nothing at all readable is a different matter: an empty redacted
        # bundle beside a raw one invites sending the wrong file.
        out.close()
        if members == 0:
            working.unlink(missing_ok=True)
            raise ValueError(
                f"{source.name} could not be read as an archive at all ({exc}). "
                f"The raw download is still stored."
            ) from exc
        truncated = True
    except BaseException:
        out.close()
        working.unlink(missing_ok=True)
        raise
    else:
        out.close()

    if truncated:
        context.progress(
            band_to,
            f"Redacted {kept} file(s) — the bundle ends early",
        )

    return store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=store_name or redacted_name(source.name),
        media_type=source.media_type,
        source=working,
    )


class _IncompleteMember(Exception):
    """A member whose body ran out before its header said it should.

    Raised rather than written, because a header promising bytes that never
    arrive is what makes an otherwise-salvageable archive unreadable.
    """


def _mask_stream(body, rules, counts: dict[str, int], declared: int) -> tuple[bytes, bool]:
    """Mask one archive member, returning its new body.

    A member is held in memory where the whole bundle never is: tar needs a
    member's final size in its header before writing it, and the largest member
    in a measured bundle is a single rotated log, not the 433 MB total. A member
    too large for that is filtered out by ``_is_binary`` before reaching here.

    ``surrogateescape`` so one malformed byte in a log does not fail a
    collection: the byte survives the round trip untouched.
    """
    out: list[str] = []
    read = 0
    for raw in body:
        read += len(raw)
        line = raw.decode("utf-8", errors="surrogateescape")
        for rule in rules:
            line, hits = rule.apply(line)
            if hits:
                counts[rule.name] = counts.get(rule.name, 0) + hits
        out.append(line)
    # Short of what the header declared means the source ran out mid-member.
    return "".join(out).encode("utf-8", errors="surrogateescape"), read >= declared


class _BytesReader:
    """The minimal file-like ``tarfile.addfile`` needs for an in-memory body.

    Deliberately not ``io.BytesIO``: that copies the bytes a second time, and
    the only method used is ``read``.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _is_binary(member: tarfile.TarInfo) -> bool:
    """True when a member should be copied through rather than masked."""
    if member.size > _MAX_REDACTABLE_MEMBER_BYTES:
        return True
    return member.name.lower().endswith(_BINARY_SUFFIXES)


def _redact_text_artifact(
    context: JobContext,
    source: Artifact,
    enabled,
    counts: dict[str, int],
) -> Artifact:
    """Mask a plain-text artifact into a stored copy.

    Reads and writes a line at a time, so the audit trail is never held whole.
    """
    source_path = artifact_path(context.data_dir, source.job_id, source.id)
    if not source_path.is_file():
        raise ValueError(f"The body of {source.name} is missing from the data volume.")

    working = artifact_path(context.data_dir, context.job_id, "audit.tmp")
    working.parent.mkdir(parents=True, exist_ok=True)
    rules = active_rules(enabled)

    try:
        with (
            source_path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as reader,
            working.open("w", encoding="utf-8", errors="surrogateescape", newline="") as out,
        ):
            for line in reader:
                for rule in rules:
                    line, hits = rule.apply(line)
                    if hits:
                        counts[rule.name] = counts.get(rule.name, 0) + hits
                out.write(line)
    except BaseException:
        working.unlink(missing_ok=True)
        raise

    return store_file(
        context.conn,
        context.data_dir,
        job_id=context.job_id,
        name=redacted_name(source.name),
        media_type=source.media_type,
        source=working,
    )


def build_report(
    *,
    host_id: str,
    host_name: str,
    enabled,
    counts: dict[str, int],
    raw: list[Artifact],
    redacted: list[Artifact],
) -> dict[str, Any]:
    """The collection's redaction report as plain JSON.

    The same shape ``job_redact`` writes — so the jobs page renders both with
    one code path — with the host and the raw/redacted file pairs added. Every
    rule appears, including ones that matched nothing and ones switched off,
    because "was this masked?" is the question someone about to send a bundle
    to Vates is actually asking.
    """
    from app.job_redact import build_report as _redact_report

    # Reuses job_redact's rule rows rather than rebuilding them, so a rule
    # added to RULES appears in both reports without this being edited.
    template = _redact_report(
        source=raw[0],
        redacted=redacted[0],
        enabled=enabled,
        counts=counts,
        lines=0,
    )

    return {
        **template,
        "host": {"id": host_id, "name": host_name},
        "files": [
            {
                "raw": _file_row(raw_item),
                "redacted": _file_row(redacted_item),
            }
            for raw_item, redacted_item in zip(raw, redacted, strict=True)
        ],
        "created_at": time.time(),
    }


def _file_row(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "name": artifact.name,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _safe_name(name: str) -> str:
    """A host's name reduced to something usable in a filename.

    The name comes from Xen Orchestra and is operator-supplied, so it can hold
    anything. Only the display name is affected — the file on disk is named by
    a generated uuid either way, per the artifact store.
    """
    cleaned = "".join(char if char.isalnum() or char in "-_." else "-" for char in name.strip())
    # Runs of dots collapse to one: a name of "../.." survives the character
    # filter intact, and a display name containing ".." reads as a path
    # traversal to whoever sees it even though the file on disk is a uuid.
    cleaned = ".".join(part for part in cleaned.split(".") if part)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:60] or "host"


def _duration(seconds: float) -> str:
    """A number of seconds as an ETA, in the largest sensible unit."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


register(KIND, run)
