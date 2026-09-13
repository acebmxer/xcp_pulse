"""Self-update: check GHCR for a newer image, and apply it in place.

Modelled on the mechanism in the sibling project beacon_pxe, whose hard-won
details are worth copying rather than rediscovering — see docs/roadmap.md for
the reasoning this file follows:

- Compare image digests, not version strings — what is deployed is read from
  the running container through the Docker socket, not remembered in the
  database, so a manual `docker compose pull && up -d` on the host cannot
  desync the two.
- Hand the recreation to a throwaway container outside the compose project —
  a container cannot reliably replace itself.
- Confirm success from the replacement container, not the initiator, since
  the process that starts an update does not survive to see it finish.

One thing differs from that model: beacon_pxe watches a channel tag
("latest" or "stable") because its release workflow publishes one on purpose.
This project's workflow (.github/workflows/release.yml) does not spell out a
`latest` tag in its own metadata-action config, but docker/metadata-action
applies one by default whenever a semver release is the newest on the
default branch — verified against GHCR: `ghcr.io/acebmxer/xcp_pulse:latest`
carries the same digest as the newest version tag. So `latest` is watched
here exactly as beacon_pxe watches its channel tag; there is no separate
channel setting because this project only ever publishes the one channel.

Also different: one container, not a stack. beacon_pxe's recreation has to
bring up six services in the right order; here there is exactly one, so the
recreator below is `docker compose up -d` with nothing else to sequence.

Applying still needs the Docker socket, which is effectively host root — the
opposite of this project's normal footprint. That is why every function here
is a no-op (or refuses outright) unless XCP_PULSE_ENABLE_SELF_UPDATE is on:
this module must be safe to import into every deployment, including the ones
that never mount the socket at all.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request

from app.config import Settings

log = logging.getLogger("xcp_pulse.update")

_OWNER = "acebmxer"
_IMAGE = "xcp_pulse"
_TAG = "latest"
_CHECK_INTERVAL = 86400  # 24 hours

# How long "Update applied successfully" stays on screen — long enough for the
# operator to see the restart finished, short enough that it isn't stale
# reassurance about an update they've long since moved on from. Failures are
# not expired: they describe a condition that is still true.
_SUCCESS_TTL = 1800  # 30 minutes

# Name of the throwaway container that performs the recreation, and how long to
# wait for it to replace this container before declaring the update stalled.
_UPDATER_NAME = "xcp_pulse_updater"
_RECREATE_TIMEOUT = 180  # 3 minutes — one service, not six.

# This container's fixed name (container_name in docker-compose.yml): how this
# process finds its own container, and the image it runs, via the socket.
_WEB_CONTAINER = "xcp-pulse"


class UpdateState:
    """A plain read-only view of one row of update_state, for callers.

    Not a frozen dataclass: constructed straight from a sqlite3.Row so a
    column added later needs no matching change here.
    """

    __slots__ = ("latest_digest", "latest_checked_at", "available", "in_progress", "last_result")

    def __init__(self, row: sqlite3.Row, *, last_result: str) -> None:
        self.latest_digest = row["latest_digest"] or ""
        self.latest_checked_at = row["latest_checked_at"]
        self.available = bool(row["available"])
        self.in_progress = bool(row["in_progress"])
        self.last_result = last_result


def image_ref() -> str:
    """Fully qualified image the update check watches and compose pulls."""
    return f"ghcr.io/{_OWNER}/{_IMAGE}:{_TAG}"


def _row(conn: sqlite3.Connection) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO update_state (id) VALUES (1)")
    conn.commit()
    return conn.execute("SELECT * FROM update_state WHERE id = 1").fetchone()


def _set(conn: sqlite3.Connection, **fields: object) -> None:
    conn.execute("INSERT OR IGNORE INTO update_state (id) VALUES (1)")
    columns = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE update_state SET {columns} WHERE id = 1", tuple(fields.values()))
    conn.commit()


def _set_result(conn: sqlite3.Connection, value: str) -> None:
    _set(conn, last_result=value, last_result_at=time.time())


def current_state(conn: sqlite3.Connection) -> UpdateState:
    """The update state worth showing, with a stale success hidden.

    A success older than _SUCCESS_TTL is treated as absent, the same as a run
    that finished before this feature existed and has no timestamp at all —
    both are results from a deployment the operator has already moved past.
    """
    row = _row(conn)
    last_result = row["last_result"] or ""
    if last_result == "success":
        stamped = row["last_result_at"]
        if not stamped or (time.time() - stamped) >= _SUCCESS_TTL:
            last_result = ""
    return UpdateState(row, last_result=last_result)


def clear_result(conn: sqlite3.Connection) -> None:
    """Drop the recorded outcome so the UI stops showing it."""
    _set_result(conn, "")


def _ghcr_latest_digest() -> str | None:
    """Return the `latest` tag's manifest digest from GHCR, or None on error.

    A network failure and a registry that has never published this image both
    return None and leave the recorded state alone — pointing this at a repo
    with nothing published yet must not report "up to date" when nothing was
    actually checked.
    """
    try:
        token_url = f"https://ghcr.io/token?scope=repository:{_OWNER}/{_IMAGE}:pull&service=ghcr.io"
        with urllib.request.urlopen(token_url, timeout=15) as r:
            token = json.loads(r.read())["token"]

        req = urllib.request.Request(
            f"https://ghcr.io/v2/{_OWNER}/{_IMAGE}/manifests/{_TAG}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Docker-Content-Digest")
    except Exception as exc:
        log.debug("GHCR digest check failed: %s", exc)
        return None


def _docker(args: list[str], timeout: int = 30) -> str | None:
    """Run a docker CLI command; trimmed stdout, or None on any failure."""
    try:
        proc = subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _deployed_digests() -> set[str] | None:
    """Registry digests of the image the running container was created from.

    Asking docker beats any recorded value: it stays correct through a manual
    `docker compose pull && up -d` on the host, which does not pass through
    this code at all. Returns None when no digest can be determined — a
    locally built dev image (no RepoDigests, see docker-compose.dev.yml), or
    docker not answering.
    """
    image_id = _docker(["inspect", _WEB_CONTAINER, "--format", "{{.Image}}"])
    if not image_id:
        return None
    raw = _docker(["image", "inspect", image_id, "--format", "{{json .RepoDigests}}"])
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        return None
    prefix = f"ghcr.io/{_OWNER}/{_IMAGE}@"
    digests = {e[len(prefix) :] for e in entries if e.startswith(prefix)}
    return digests or None


def check_for_updates(conn: sqlite3.Connection, *, is_dev_build: bool = False) -> bool:
    """Query GHCR and record the result. Returns True if an update is available.

    A failed check (network down, GHCR unreachable) keeps whatever was last
    recorded rather than guessing — it must not retract a real pending update
    just because this one attempt couldn't reach the registry.

    is_dev_build (from Settings.is_dev_build — set only by
    docker-compose.dev.yml's build arg, see the Dockerfile) means this image
    was built locally rather than pulled from GHCR. A bare digest comparison
    cannot tell that apart from genuinely being behind: docker-compose.dev.yml
    deliberately tags a local build with the same ghcr.io/... name the
    published image uses, and that local build still gets a real, different
    digest the moment anything in the source changed — including changes
    that are ahead of the published image, not behind it. So a dev build
    never reports available on its own account; it still records the latest
    digest seen (so Check now/the page have something to show), and the
    result stays whatever it already was otherwise, exactly as a failed
    network check does.
    """
    _set(conn, latest_checked_at=time.time())

    digest = _ghcr_latest_digest()
    if digest is None:
        return current_state(conn).available

    deployed = _deployed_digests()
    if deployed is None:
        # Docker isn't answering, or this is a locally built dev image with no
        # registry digest to compare against — nothing to report either way.
        _set(conn, latest_digest=digest)
        return current_state(conn).available

    if is_dev_build:
        _set(conn, latest_digest=digest)
        return current_state(conn).available

    available = digest not in deployed
    _set(conn, latest_digest=digest, available=1 if available else 0)
    return available


def _spawn_recreator(image: str, project_dir: str) -> str | None:
    """Start a throwaway container to run `docker compose pull && up -d`.

    The recreation cannot run in this process: `docker compose up -d` stays in
    the foreground doing the recreation, and the one container it must replace
    is the one this code is running in — killing this process kills the
    compose run mid-update. So the job goes to a container outside the
    compose project, which nothing in this stack can take down. It runs the
    image just pulled (guaranteed present, and it carries the docker CLI +
    compose plugin for exactly this reason) with the project directory
    mounted at its own path, so compose resolves the same project name and
    the same relative volume paths the original `up` did.

    The pull happens here too, not in run_update before this is spawned, and
    that is deliberate: pulling from run_update's own container would need
    docker-compose.yml and xcp-pulse.env resolved from *that* container's
    filesystem view, which is a different path than the host's — compose's
    --project-directory only changes where relative paths in the compose file
    resolve, it does not relocate the files themselves. The recreator already
    has the project directory mounted at its real host path, so running both
    steps here means everything compose reads is exactly where it expects.

    The recreator inherits this process's supplementary groups (via --group-add
    for each one) rather than running as root, matching the non-root user the
    image runs as throughout. Without this, the recreator can see the mounted
    socket but every call to it fails with "permission denied" — the exact
    failure this process would hit too, if it hadn't been launched the same
    way (see docker-compose.yml.example's group_add comment). group_add takes
    numeric GIDs, so this passes them straight through without needing the
    recreator's own /etc/group to know the host's docker group by name.

    Returns None once the container is launched, or an error string.
    """
    # The previous run's exited container is expected here — no --rm below, so
    # its exit code and logs stay inspectable after it finishes.
    subprocess.run(
        ["docker", "rm", "-f", _UPDATER_NAME], capture_output=True, text=True, timeout=30
    )

    group_args = []
    for gid in os.getgroups():
        group_args += ["--group-add", str(gid)]

    launch = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            _UPDATER_NAME,
            *group_args,
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{project_dir}:{project_dir}",
            "-w",
            project_dir,
            image,
            "sh",
            "-c",
            "docker compose pull && docker compose up -d",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if launch.returncode != 0:
        return (launch.stderr or launch.stdout or "unknown error").strip()[:300]
    return None


def finish_pending_update(conn: sqlite3.Connection) -> None:
    """Resolve an in-flight update at startup.

    Called from the app's startup hook. Reaching this point with an update in
    progress means this process is the *replacement* container: the previous
    one was stopped by the recreation, and the new image is now running. That
    is the only trustworthy confirmation available, since the process that
    started the update does not survive to see it finish.
    """
    if not current_state(conn).in_progress:
        return
    _set(conn, available=0, in_progress=0)
    _set_result(conn, "success")
    log.info("Update completed; now running the recreated container")


def reap_stalled_update(conn: sqlite3.Connection) -> None:
    """Fail an update that started but never replaced this container.

    If the recreation dies, this process keeps running with the update still
    marked in progress, and the UI would spin on "pulling images" forever.
    finish_pending_update clears the flag on startup, so anything still set
    here well past that point means the recreation never happened.
    """
    row = _row(conn)
    if not row["in_progress"] or not row["started_at"]:
        return
    if (time.time() - row["started_at"]) < _RECREATE_TIMEOUT:
        return

    _set(conn, in_progress=0)
    _set_result(
        conn,
        "recreate_failed: the image was pulled but the container was not "
        "recreated. Run `docker compose up -d` on the host to finish.",
    )
    log.warning("Update stalled: the container was not recreated in time")


def run_update(settings: Settings, db_path) -> None:
    """Hand pulling and recreation to a throwaway container.

    Runs on its own thread with its own connection, the same as the job
    worker — a request thread cannot own a long-running background operation
    that is meant to outlive the container this request is served from.

    Neither the pull nor the recreation happen in this process. Pulling here
    would need docker-compose.yml and xcp-pulse.env resolved from *this*
    container's filesystem view — a different path than the host's, since
    this container only has them bind-mounted in for reading, not at the
    same absolute path the host runs compose from — so the recreator does
    both, from its mount of the real project directory. See
    _spawn_recreator's docstring.

    When this container is the one about to be replaced, no success is
    recorded here: this process is about to be killed by the recreation and
    cannot observe the outcome. The replacement container confirms it
    instead, in finish_pending_update. Only when this container's own image
    is already current — nothing will replace it — does this process survive
    to watch the recreator and record the outcome itself.
    """
    from app.db import connect  # local import: avoids a cycle with app.db

    conn = connect(db_path)
    try:
        state = current_state(conn)
        _set(conn, in_progress=1, started_at=time.time())
        _set_result(conn, "")

        project_dir = settings.compose_project_dir

        if not project_dir:
            _set_result(
                conn,
                "recreate_failed: XCP_PULSE_COMPOSE_PROJECT_DIR is not set, so the "
                "update cannot recreate the container. Set it and run "
                "`docker compose up -d` on the host.",
            )
            _set(conn, in_progress=0)
            return

        # Will the recreation replace *this* container? Decided from what's
        # already known — the digest the last check found deployed, versus
        # the digest it found on GHCR — rather than pulling again here to find
        # out, for the filesystem-view reason above. A stale answer only
        # causes this process to watch a recreation a moment longer than
        # necessary or hand off one it could have watched itself; either way
        # the replacement container's own startup is still the source of
        # truth for success, per finish_pending_update.
        deployed = _deployed_digests()
        web_unchanged = bool(
            deployed is not None and state.latest_digest and state.latest_digest in deployed
        )

        error = _spawn_recreator(image_ref(), project_dir)
        if error:
            _set_result(conn, f"recreate_failed: {error}")
            _set(conn, in_progress=0)
            return

        if web_unchanged:
            # This process survives the recreation, so the recreator can be
            # watched directly to its exit.
            try:
                wait = subprocess.run(
                    ["docker", "wait", _UPDATER_NAME],
                    capture_output=True,
                    text=True,
                    timeout=_RECREATE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                _set_result(
                    conn,
                    "recreate_failed: the recreation is still running after "
                    f"{_RECREATE_TIMEOUT}s. Check `docker logs {_UPDATER_NAME}` on the host.",
                )
                _set(conn, in_progress=0)
                return

            code = (wait.stdout or "").strip()
            if wait.returncode == 0 and code == "0":
                subprocess.run(
                    ["docker", "rm", _UPDATER_NAME], capture_output=True, text=True, timeout=30
                )
                _set(conn, available=0, in_progress=0)
                _set_result(conn, "success")
                log.info("Update finished; this container was already current")
            else:
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "5", _UPDATER_NAME],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                detail = ((logs.stderr or "") + (logs.stdout or "")).strip()
                detail = detail or f"updater exit code {code or wait.returncode}"
                _set_result(conn, f"recreate_failed: {detail[:300]}")
                _set(conn, in_progress=0)
            return

        # This container is now living on borrowed time — the recreation will
        # stop it shortly. Its replacement writes the success state.

    except subprocess.TimeoutExpired:
        _set_result(conn, "timeout: a docker command took too long")
        _set(conn, in_progress=0)
    except Exception as exc:
        _set_result(conn, f"error: {str(exc)[:200]}")
        _set(conn, in_progress=0)
    finally:
        conn.close()


def start_background_checker(settings: Settings, db_path) -> None:
    """Run check_for_updates once a day, on its own thread and connection.

    A no-op unless self-update is enabled — the outbound call this makes is
    itself a small opt-in (this is the only network call this project makes
    to somewhere that isn't the configured Xen Orchestra), not something every
    deployment should do by default.
    """
    if not settings.enable_self_update:
        return

    from app.db import connect  # local import: avoids a cycle with app.db

    def _loop() -> None:
        time.sleep(300)  # let the app settle before the first network call
        conn = connect(db_path)
        try:
            while True:
                try:
                    check_for_updates(conn, is_dev_build=settings.is_dev_build)
                except Exception:
                    log.exception("background update check failed")
                time.sleep(_CHECK_INTERVAL)
        finally:
            conn.close()

    threading.Thread(target=_loop, daemon=True, name="update-checker").start()
