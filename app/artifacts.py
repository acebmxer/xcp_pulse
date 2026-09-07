"""What a job produced: a file on the data volume, described by a database row.

Splitting the two is what lets one store hold both the few hundred bytes of
JSON a "Refresh inventory" job writes and the 433 MB tarball a collection job
writes. The body is always a file; the row records what it is, how
big it is and what it hashes to, so listing artifacts never opens them.

Files live under ``<data_dir>/artifacts/<job_id>/<artifact_id>``. The name the
operator sees is stored in the row rather than used as the filename, so a name
coming from Xen Orchestra can never escape the directory or collide.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Read and hash in chunks so a 433 MB bundle never has to be held in memory.
_CHUNK_BYTES = 1024 * 1024

JSON_MEDIA_TYPE = "application/json"


def human_bytes(size: int) -> str:
    """A byte count as something to put on a page.

    Module-level rather than only a property because a total — a collection's
    files summed, a retention plan's freed space — has no Artifact to ask, and
    a second copy of this formatting would drift from the first. Anything
    showing a size calls this or ``Artifact.size_human``, which is this.

    Units are binary and are labelled as such. Dividing by 1024 and printing
    "MB" made a 454 MB download read as "426.0 MB" on the collect page, which
    is the one number an operator checks against what they expected to arrive —
    and a download that appears to stop 28 MB short of a bundle the docs call
    433 MB looks like a truncation rather than a complete transfer.
    """
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if unit == "B":
            if value < 1024:
                return f"{int(value)} B"
        elif value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


@dataclass(frozen=True)
class Artifact:
    """One stored result, without its body."""

    id: str
    job_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: float

    @property
    def size_human(self) -> str:
        """The size as something to put on a page.

        The exact byte count stays in ``size_bytes`` for anything that needs
        it; nobody reading a job result needs three decimal places.
        """
        return human_bytes(self.size_bytes)


def artifacts_dir(data_dir: Path) -> Path:
    """The directory holding every artifact body. Created if absent."""
    path = data_dir / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(data_dir: Path, job_id: str, artifact_id: str) -> Path:
    """Where one artifact's body lives.

    Both ids are uuid4 hex generated here rather than supplied by a caller,
    which is what makes joining them onto a path safe.
    """
    return artifacts_dir(data_dir) / job_id / artifact_id


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        job_id=row["job_id"],
        name=row["name"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


def store_file(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    job_id: str,
    name: str,
    media_type: str,
    source: Path,
    move: bool = True,
) -> Artifact:
    """Take a file already on disk into the store and record it.

    ``move`` is the default because the caller that will matter most — log
    collection — writes a 433 MB download to a temporary path and has no reason
    to copy it a second time. The hash is computed by reading the file in
    chunks, never by loading it.
    """
    artifact_id = uuid.uuid4().hex
    destination = artifact_path(data_dir, job_id, artifact_id)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if move:
        shutil.move(str(source), str(destination))
    else:
        shutil.copyfile(source, destination)

    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)

    artifact = Artifact(
        id=artifact_id,
        job_id=job_id,
        name=name,
        media_type=media_type,
        size_bytes=destination.stat().st_size,
        sha256=digest.hexdigest(),
        created_at=time.time(),
    )
    _insert(conn, artifact)
    return artifact


def store_json(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    job_id: str,
    name: str,
    payload: object,
) -> Artifact:
    """Store a JSON-serialisable result as an artifact.

    Deliberately not a second store: it writes the file and then hands over to
    the same recording path as every other artifact, so a JSON result and a log
    bundle are listed, hashed and deleted by identical code.
    """
    artifact_id = uuid.uuid4().hex
    destination = artifact_path(data_dir, job_id, artifact_id)
    destination.parent.mkdir(parents=True, exist_ok=True)

    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    destination.write_bytes(body)

    artifact = Artifact(
        id=artifact_id,
        job_id=job_id,
        name=name,
        media_type=JSON_MEDIA_TYPE,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        created_at=time.time(),
    )
    _insert(conn, artifact)
    return artifact


def _insert(conn: sqlite3.Connection, artifact: Artifact) -> None:
    conn.execute(
        """
        INSERT INTO artifacts
            (id, job_id, name, media_type, size_bytes, sha256, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact.id,
            artifact.job_id,
            artifact.name,
            artifact.media_type,
            artifact.size_bytes,
            artifact.sha256,
            artifact.created_at,
        ),
    )
    conn.commit()


def list_for_job(conn: sqlite3.Connection, job_id: str) -> list[Artifact]:
    """Every artifact one job produced, oldest first."""
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at",
        (job_id,),
    ).fetchall()
    return [_row_to_artifact(row) for row in rows]


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    """One artifact's metadata, or None when it is not stored."""
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return None if row is None else _row_to_artifact(row)


def read_json(data_dir: Path, artifact: Artifact) -> object:
    """Read a JSON artifact's body back.

    Only for artifacts this application wrote as JSON. A bundle is served as a
    file rather than read into memory, which is why there is no general
    ``read`` here.
    """
    path = artifact_path(data_dir, artifact.job_id, artifact.id)
    return json.loads(path.read_text(encoding="utf-8"))


def delete_for_job(conn: sqlite3.Connection, data_dir: Path, job_id: str) -> int:
    """Remove one job's artifact files and rows. Returns the count removed.

    The files go first: a row with no file is a broken download, but a file
    with no row is invisible and never reclaimed. The rows would also be taken
    by the foreign key when the job row goes; doing it here means the files go
    with them.
    """
    artifacts = list_for_job(conn, job_id)
    for artifact in artifacts:
        artifact_path(data_dir, job_id, artifact.id).unlink(missing_ok=True)

    job_dir = artifacts_dir(data_dir) / job_id
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)

    conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
    conn.commit()
    return len(artifacts)
