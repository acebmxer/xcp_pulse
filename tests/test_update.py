"""Self-update state tracking and the digest comparison it's built on.

run_update's actual docker calls aren't exercised here — that needs a real
daemon and socket, which is exactly why this feature is opt-in. What's
tested is everything check_for_updates, current_state and the reap/finish
lifecycle do with the database, since those are what the UI and the startup
hook actually depend on, and they don't need docker to be right.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.db import connect, migrate
from app.update import (
    check_for_updates,
    clear_result,
    current_state,
    finish_pending_update,
    image_ref,
    reap_stalled_update,
)


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    migrate(connection)
    # The row is created lazily on first touch in production (see
    # app/update.py's _row/_set) — force that here so a test's own direct SQL
    # against update_state has a row to update rather than silently matching
    # zero rows.
    connection.execute("INSERT INTO update_state (id) VALUES (1)")
    connection.commit()
    yield connection
    connection.close()


def test_image_ref_is_the_latest_tag() -> None:
    assert image_ref() == "ghcr.io/acebmxer/xcp_pulse:latest"


def test_fresh_state_is_up_to_date_and_never_checked(conn) -> None:
    state = current_state(conn)
    assert state.available is False
    assert state.in_progress is False
    assert state.latest_checked_at is None
    assert state.last_result == ""


def test_check_reports_available_when_digest_differs(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:new")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:old"})

    assert check_for_updates(conn) is True
    state = current_state(conn)
    assert state.available is True
    assert state.latest_digest == "sha256:new"
    assert state.latest_checked_at is not None


def test_check_reports_up_to_date_when_digest_matches(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:same")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:same"})

    assert check_for_updates(conn) is False
    assert current_state(conn).available is False


def test_check_keeps_prior_state_when_registry_unreachable(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed check must not silently retract or invent an update."""
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:new")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:old"})
    check_for_updates(conn)
    assert current_state(conn).available is True

    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: None)
    assert check_for_updates(conn) is True
    assert current_state(conn).available is True


def test_check_does_not_claim_available_with_no_deployed_digest(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev build with no RepoDigests, or docker not answering, must not be
    reported as needing an update — there is nothing to compare against."""
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:new")
    monkeypatch.setattr("app.update._deployed_digests", lambda: None)

    assert check_for_updates(conn) is False
    assert current_state(conn).available is False


def test_check_does_not_claim_available_for_a_dev_build_even_with_a_real_deployed_digest(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locally built image (docker-compose.dev.yml) keeps the same
    ghcr.io/... tag the published image uses, so it genuinely does have a
    real, different RepoDigest once built — unlike the no-digest case above.
    That difference means "ahead of :latest" just as often as "behind it",
    so is_dev_build must suppress the claim even though deployed digests are
    available and do differ from the registry's.
    """
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:published")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:local-build"})

    assert check_for_updates(conn, is_dev_build=True) is False
    state = current_state(conn)
    assert state.available is False
    # Still recorded, so the page has something to show (last checked, etc).
    assert state.latest_digest == "sha256:published"


def test_check_still_reports_available_for_a_non_dev_build(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_dev_build defaults to False — a normal deployment is unaffected."""
    monkeypatch.setattr("app.update._ghcr_latest_digest", lambda: "sha256:new")
    monkeypatch.setattr("app.update._deployed_digests", lambda: {"sha256:old"})

    assert check_for_updates(conn) is True
    assert current_state(conn).available is True


def test_finish_pending_update_records_success_and_clears_flags(conn) -> None:
    conn.execute("UPDATE update_state SET in_progress = 1, available = 1 WHERE id = 1")
    conn.commit()

    finish_pending_update(conn)

    state = current_state(conn)
    assert state.in_progress is False
    assert state.available is False
    assert state.last_result == "success"


def test_finish_pending_update_is_a_no_op_when_nothing_was_in_progress(conn) -> None:
    finish_pending_update(conn)
    assert current_state(conn).last_result == ""


def test_reap_stalled_update_fails_after_timeout(conn) -> None:
    conn.execute(
        "UPDATE update_state SET in_progress = 1, started_at = ? WHERE id = 1",
        (time.time() - 999,),
    )
    conn.commit()

    reap_stalled_update(conn)

    state = current_state(conn)
    assert state.in_progress is False
    assert "recreate_failed" in state.last_result


def test_reap_stalled_update_leaves_a_recent_update_alone(conn) -> None:
    conn.execute(
        "UPDATE update_state SET in_progress = 1, started_at = ? WHERE id = 1",
        (time.time(),),
    )
    conn.commit()

    reap_stalled_update(conn)

    state = current_state(conn)
    assert state.in_progress is True
    assert state.last_result == ""


def test_success_result_expires_after_its_ttl(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.update as update_mod

    monkeypatch.setattr(update_mod, "_SUCCESS_TTL", 1)
    conn.execute(
        "UPDATE update_state SET last_result = 'success', last_result_at = ? WHERE id = 1",
        (time.time() - 5,),
    )
    conn.commit()

    assert current_state(conn).last_result == ""


def test_failure_result_does_not_expire(conn) -> None:
    conn.execute(
        "UPDATE update_state SET last_result = 'pull_failed: no route to host', "
        "last_result_at = ? WHERE id = 1",
        (time.time() - 999999,),
    )
    conn.commit()

    assert current_state(conn).last_result == "pull_failed: no route to host"


def test_clear_result_empties_it(conn) -> None:
    conn.execute(
        "UPDATE update_state SET last_result = 'success', last_result_at = ? WHERE id = 1",
        (time.time(),),
    )
    conn.commit()

    clear_result(conn)

    assert current_state(conn).last_result == ""
