"""The artifact store: bodies on disk, metadata in the database.

The split is what lets one store hold both the JSON an inventory refresh writes
and the 433 MB tarball collection will write later, so these check that a file
artifact and a JSON artifact are recorded, listed and deleted by the same code.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.artifacts import (
    JSON_MEDIA_TYPE,
    Artifact,
    artifact_path,
    delete_for_job,
    get_artifact,
    list_for_job,
    read_json,
    store_file,
    store_json,
)
from app.db import init_db
from app.jobs import enqueue


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


@pytest.fixture
def job_id(conn: sqlite3.Connection) -> str:
    return enqueue(conn, "refresh_inventory").id


def test_json_is_stored_hashed_and_read_back(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    payload = {"pools": [{"id": "pool-1", "name": "Pool1"}], "hosts": []}
    artifact = store_json(conn, tmp_path, job_id=job_id, name="inventory.json", payload=payload)

    assert artifact.media_type == JSON_MEDIA_TYPE
    assert artifact.size_bytes > 0
    assert len(artifact.sha256) == 64
    assert read_json(tmp_path, artifact) == payload


def test_a_file_is_moved_into_the_store_and_hashed(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    """The path collection will use: a downloaded file taken over, not copied."""
    source = tmp_path / "downloaded.tgz"
    source.write_bytes(b"pretend-bundle" * 1000)

    artifact = store_file(
        conn,
        tmp_path,
        job_id=job_id,
        name="logs.tgz",
        media_type="application/gzip",
        source=source,
    )

    assert not source.exists(), "the source must be moved, not left behind as a second copy"
    assert artifact.size_bytes == 14000
    stored = artifact_path(tmp_path, job_id, artifact.id)
    assert stored.read_bytes() == b"pretend-bundle" * 1000


def test_a_copied_file_leaves_the_original_alone(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    source = tmp_path / "keep-me.txt"
    source.write_text("evidence")

    store_file(
        conn,
        tmp_path,
        job_id=job_id,
        name="keep-me.txt",
        media_type="text/plain",
        source=source,
        move=False,
    )
    assert source.exists()


def test_the_stored_name_is_not_used_as_the_filename(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    """A name from Xen Orchestra must not be able to choose a path.

    The body is written under the generated artifact id; the display name lives
    in the row, so a name containing separators cannot escape the directory.
    """
    artifact = store_json(
        conn, tmp_path, job_id=job_id, name="../../etc/passwd", payload={"ok": True}
    )

    stored = artifact_path(tmp_path, job_id, artifact.id)
    assert stored.is_file()
    assert artifact.id in stored.name
    assert artifact.name == "../../etc/passwd", "the name is kept for display, just not used"


def test_artifacts_are_listed_for_their_job_oldest_first(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    store_json(conn, tmp_path, job_id=job_id, name="first.json", payload={"n": 1})
    store_json(conn, tmp_path, job_id=job_id, name="second.json", payload={"n": 2})

    assert [item.name for item in list_for_job(conn, job_id)] == ["first.json", "second.json"]


def test_one_jobs_artifacts_are_not_another_jobs(conn: sqlite3.Connection, tmp_path: Path) -> None:
    first = enqueue(conn, "refresh_inventory").id
    second = enqueue(conn, "refresh_inventory").id
    store_json(conn, tmp_path, job_id=first, name="a.json", payload={})

    assert len(list_for_job(conn, first)) == 1
    assert list_for_job(conn, second) == []


def test_deleting_takes_the_files_as_well_as_the_rows(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    """A row with no file is a broken download; a file with no row is never
    reclaimed. Deleting has to take both."""
    artifact = store_json(conn, tmp_path, job_id=job_id, name="inventory.json", payload={})
    path = artifact_path(tmp_path, job_id, artifact.id)
    assert path.exists()

    assert delete_for_job(conn, tmp_path, job_id) == 1
    assert not path.exists()
    assert list_for_job(conn, job_id) == []
    assert get_artifact(conn, artifact.id) is None


def test_deleting_a_job_row_takes_its_artifact_rows(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    """The foreign key, which is why the schema turns foreign_keys on."""
    store_json(conn, tmp_path, job_id=job_id, name="inventory.json", payload={})
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()

    assert list_for_job(conn, job_id) == []


def test_read_json_survives_a_body_that_is_gone(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    artifact = store_json(conn, tmp_path, job_id=job_id, name="inventory.json", payload={})
    artifact_path(tmp_path, job_id, artifact.id).unlink()

    with pytest.raises(OSError):
        read_json(tmp_path, artifact)


def test_the_json_body_on_disk_is_valid_json(
    conn: sqlite3.Connection, tmp_path: Path, job_id: str
) -> None:
    artifact = store_json(conn, tmp_path, job_id=job_id, name="x.json", payload={"a": 1})
    raw = artifact_path(tmp_path, job_id, artifact.id).read_text(encoding="utf-8")
    assert json.loads(raw) == {"a": 1}


def test_size_is_shown_in_readable_units() -> None:
    def sized(n: int) -> str:
        return Artifact("i", "j", "n", "t", n, "s", 0.0).size_human

    assert sized(512) == "512 B"
    assert sized(1536) == "1.5 KiB"
    assert sized(454 * 1024 * 1024) == "454.0 MiB"
    assert sized(3 * 1024**3) == "3.0 GiB"

    # The units are binary and say so. Labelling a division by 1024 as "MB"
    # made a 454,033,408-byte download read as "426.0 MB" against a bundle the
    # docs call 433 MB, so a complete transfer looked like a truncated one.
    assert sized(454_033_408) == "433.0 MiB"
