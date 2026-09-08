"""HTTP client for the Xen Orchestra REST API.

One client for every XO call the application makes. Anything needing XO goes
through here rather than building its own requests, so authentication, TLS
handling and error translation exist in exactly one place.

Authentication is by API token, sent as the ``authenticationToken`` cookie.
XO also accepts HTTP Basic, but a token can be revoked in XO without changing
an account password, which is what we want operators to be able to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

# XO's REST API is versioned in the path; every route below is relative to it.
API_BASE = "/rest/v0"

# Downloading a host's log bundle needs its own privilege, distinct from read.
# Measured: a restricted account is refused with exactly this action named in
# the 403 body.
LOG_EXPORT_ACTION = "export:logs"
LOG_EXPORT_RESOURCE = "host"

# The two log routes. Both need LOG_EXPORT_ACTION; both are plain file bodies
# rather than JSON, and neither accepts a date parameter or a byte range, which
# is why collection downloads the whole thing and filters afterwards.
LOGS_PATH = "/hosts/{host_id}/logs.tgz"
AUDIT_PATH = "/hosts/{host_id}/audit.txt"

# How long the far end may go silent mid-transfer. httpx applies a read timeout
# per chunk rather than to the whole response, so this is not a limit on how
# long a download may take — a 433 MiB bundle measured at over three minutes
# passes fine as long as data keeps arriving.
#
# It is also how long a finished transfer waits before giving up on a
# terminator that is not coming — measured against one pool through nginx, the
# last byte arrives and the response never ends — so it has to stay short
# enough that a complete download does not look like a hang, while staying
# longer than any real gap between chunks. Xen Orchestra builds the archive as
# it streams, and the largest gap measured was a few seconds.
DOWNLOAD_TIMEOUT = 60.0

# Written to disk a megabyte at a time. Large enough that a 433 MB body is not
# 433 000 writes, small enough that cancellation is noticed promptly.
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# Granting every action on hosts implies log export. It is full host
# administration, so it is reported as such rather than as a way to stay
# restricted.
WILDCARD_ACTION = "*"

# Administrator-only because it carries no ACL middleware. Used to tell an
# administrator from a restricted account: routes that are ACL-filtered answer
# both with 200 and cannot distinguish them.
DASHBOARD_PATH = "/dashboard"

# XO returns collections as bare href strings unless "fields" is given, so
# these are what turns /pools and /hosts into displayable records. Requesting a
# field an instance does not know is harmless — it is simply absent from the
# response — so every reader below treats each one as optional.
POOL_FIELDS = "id,name_label,master"
HOST_FIELDS = "id,name_label,address,version,productBrand,power_state,enabled,$pool,cpus,memory"

# The routes findings are read from, and the fields each one needs.
#
# Every one is a plain collection route, so all of them answer a restricted
# account with ``200 []`` rather than a refusal — the same trap the inventory
# has, and the reason `_raise_for_collection` is shared with these.
MESSAGES_PATH = "/messages"
MESSAGE_FIELDS = "id,name,body,time,$object,$pool"

TASKS_PATH = "/tasks"
TASK_FIELDS = "id,status,start,end,result,properties"

ALARMS_PATH = "/alarms"
ALARM_FIELDS = "id,name,body,time,$object,$pool"

BACKUP_LOGS_PATH = "/backup/logs"
RESTORE_LOGS_PATH = "/restore/logs"
BACKUP_LOG_FIELDS = "id,jobId,jobName,status,start,end,message"

MISSING_PATCHES_PATH = "/pools/{pool_id}/missing_patches"

# How the event routes are bounded to a time window.
#
# **``limit`` cannot be used for this.** Measured: XO applies it to the oldest
# records, not the newest — asking /messages for 2000 of 3,472 rows returned
# everything from the *first* month and nothing from the last, silently hiding
# every recent finding. ``sort`` and ``order`` are accepted and ignored.
#
# ``filter`` is applied server-side and does work, so the window is expressed
# as a filter and the whole matching set is returned. That also means the
# response shrinks with the window rather than growing with pool age.
#
# The two timestamp scales are XO's, not ours: XAPI messages and alarms carry
# seconds, XO tasks and backup runs carry milliseconds. Filtering a
# millisecond field with a seconds value matches everything, which is a bug
# that looks exactly like a working filter.
MESSAGE_TIME_FIELD = "time"
TASK_TIME_FIELD = "start"

_DEFAULT_TIMEOUT = 30.0


class XoError(RuntimeError):
    """A Xen Orchestra call failed in a way worth showing the operator.

    Carries a message written for someone configuring the connection, not a
    stack trace — the settings page renders it directly.
    """


@dataclass(frozen=True)
class LogExportSupport:
    """Whether this connection can download host logs, and why not if it cannot.

    Kept separate from the connection test because the answer is a property of
    the account and the instance together, and the reason matters as much as
    the verdict: an operator who cannot collect logs needs to know whether to
    change the account or the role.
    """

    available: bool
    reason: str
    grantable: bool = True

    @property
    def summary(self) -> str:
        return "available" if self.available else f"not available — {self.reason}"


@dataclass(frozen=True)
class Pool:
    """One pool as the inventory sees it."""

    id: str
    name: str
    master_id: str = ""


@dataclass(frozen=True)
class Host:
    """One host as the inventory sees it."""

    id: str
    name: str
    address: str = ""
    version: str = ""
    product: str = ""
    power_state: str = ""
    enabled: bool = True
    pool_id: str = ""
    memory_used: int = 0
    memory_total: int = 0
    cpu_cores: int = 0
    cpu_sockets: int = 0

    @property
    def memory_percent(self) -> int | None:
        """Memory in use as a whole percentage, or None when unreported."""
        if self.memory_total <= 0:
            return None
        return round(self.memory_used / self.memory_total * 100)

    @property
    def running(self) -> bool:
        return self.power_state.lower() == "running"


@dataclass(frozen=True)
class Inventory:
    """The pools and hosts one account can see.

    Empty is a legitimate result rather than a failure: XO answers an account
    without read privileges with an empty list and HTTP 200. Callers decide how
    to explain that; this only reports what came back.
    """

    pools: list[Pool] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.pools and not self.hosts

    def hosts_in(self, pool_id: str) -> list[Host]:
        """The hosts belonging to one pool, named first for a stable order."""
        return sorted(
            (host for host in self.hosts if host.pool_id == pool_id),
            key=lambda host: host.name.lower(),
        )

    @property
    def orphan_hosts(self) -> list[Host]:
        """Hosts whose pool is not in the inventory.

        Possible for a restricted account: pool and host read privileges are
        granted separately, so an account can be allowed to see a host while
        being refused the pool containing it. Such a host is still shown rather
        than silently dropped.
        """
        known = {pool.id for pool in self.pools}
        return sorted(
            (host for host in self.hosts if host.pool_id not in known),
            key=lambda host: host.name.lower(),
        )


@dataclass(frozen=True)
class ConnectionTest:
    """What a "Test connection" attempt found."""

    ok: bool
    message: str
    is_admin: bool = False
    pool_count: int = 0
    host_count: int = 0
    log_export: LogExportSupport | None = None
    warnings: list[str] = field(default_factory=list)


class XoClient:
    """Talks to one Xen Orchestra instance.

    Construct per operation rather than holding one open for the process
    lifetime: settings can change under us, and XO tokens can be revoked.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_tls: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._verify_tls = verify_tls
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{self.url}{API_BASE}",
            cookies={"authenticationToken": self._token},
            verify=self._verify_tls,
            timeout=self._timeout,
            follow_redirects=True,
        )

    def _get(self, path: str, **params: object) -> httpx.Response:
        """Perform a GET, translating transport failures into XoError.

        HTTP status is deliberately not raised on here: callers distinguish 403
        from 404 from 200, and a 403 is a normal answer to "may I?" rather than
        an exceptional condition.
        """
        try:
            with self._client() as client:
                return client.get(path, params=params or None)
        except httpx.TimeoutException as exc:
            raise XoError(f"timed out after {self._timeout:.0f}s contacting {self.url}") from exc
        except httpx.ConnectError as exc:
            raise XoError(
                f"cannot reach {self.url} — check the address, and whether TLS "
                f"verification should be disabled for a self-signed certificate"
            ) from exc
        except httpx.HTTPError as exc:
            raise XoError(f"request to {self.url} failed: {exc}") from exc

    # -- inventory -------------------------------------------------------

    def list_pools(self) -> list[str]:
        """Return the pool hrefs this account can see."""
        return _href_list(self._get("/pools"))

    def list_hosts(self) -> list[str]:
        """Return the host hrefs this account can see."""
        return _href_list(self._get("/hosts"))

    def inventory(self) -> Inventory:
        """Return the pools and hosts this account can see, with their details.

        Asking for ``fields`` is what turns a collection from a list of hrefs
        into a list of objects; without it XO answers with href strings alone,
        which is enough to count but not to display.

        An empty result is not an error and is not reported as one here — a
        restricted account is answered with ``200 []`` rather than a refusal,
        so the caller is told the inventory is empty and left to explain why.
        """
        pools = [
            Pool(
                id=str(record.get("id", "")),
                name=_label(record, "pool"),
                master_id=str(record.get("master") or ""),
            )
            for record in _record_list(self._get("/pools", fields=POOL_FIELDS))
        ]

        hosts = [
            Host(
                id=str(record.get("id", "")),
                name=_label(record, "host"),
                address=str(record.get("address") or ""),
                version=str(record.get("version") or ""),
                product=str(record.get("productBrand") or ""),
                power_state=str(record.get("power_state") or ""),
                enabled=bool(record.get("enabled", True)),
                pool_id=str(record.get("$pool") or ""),
                memory_used=_nested_int(record, "memory", "usage"),
                memory_total=_nested_int(record, "memory", "size"),
                cpu_cores=_nested_int(record, "cpus", "cores"),
                cpu_sockets=_nested_int(record, "cpus", "sockets"),
            )
            for record in _record_list(self._get("/hosts", fields=HOST_FIELDS))
        ]

        return Inventory(pools=pools, hosts=hosts)

    # -- findings sources ------------------------------------------------
    #
    # Six reads, all going through ``_get`` and ``_record_list`` above rather
    # than building their own requests, so a restricted account's ``200 []``
    # keeps being told apart from a refusal in exactly one place.

    def messages(self, since: float) -> list[dict[str, object]]:
        """XAPI messages since ``since`` (a Unix time in seconds).

        These are the pool's own event record — ``twinstor_degraded``,
        ``VDI_CBT_METADATA_INCONSISTENT``, ``POOL_MASTER_TRANSITION`` — mixed
        in with routine VM lifecycle noise. Classifying them is the findings
        module's job; this only fetches.
        """
        return self._events(MESSAGES_PATH, MESSAGE_FIELDS, MESSAGE_TIME_FIELD, since)

    def alarms(self, since: float) -> list[dict[str, object]]:
        """Active alarms since ``since`` (seconds).

        A separate route from messages even though an alarm is a message in
        XAPI terms, because XO filters it and an operator asking "what is
        alarming?" means the filtered list.
        """
        return self._events(ALARMS_PATH, ALARM_FIELDS, MESSAGE_TIME_FIELD, since)

    def tasks(self, since: float) -> list[dict[str, object]]:
        """XO tasks since ``since`` (seconds), with their failure results.

        ``properties`` is requested because it carries the task's human name
        and type, which is the only thing distinguishing one failure from
        another. It also carries request arguments — measured: usernames and
        client IP addresses — so what is kept from a task is chosen field by
        field rather than stored whole.
        """
        return self._events(TASKS_PATH, TASK_FIELDS, TASK_TIME_FIELD, since, millis=True)

    def backup_logs(self, since: float) -> list[dict[str, object]]:
        """Backup job runs since ``since`` (seconds)."""
        return self._events(
            BACKUP_LOGS_PATH, BACKUP_LOG_FIELDS, TASK_TIME_FIELD, since, millis=True
        )

    def restore_logs(self, since: float) -> list[dict[str, object]]:
        """Restore runs since ``since`` (seconds)."""
        return self._events(
            RESTORE_LOGS_PATH, BACKUP_LOG_FIELDS, TASK_TIME_FIELD, since, millis=True
        )

    def _events(
        self,
        path: str,
        fields: str,
        time_field: str,
        since: float,
        *,
        millis: bool = False,
    ) -> list[dict[str, object]]:
        """One time-bounded event read. Every event route goes through here.

        Shared so the filter is built in exactly one place: the scale
        conversion is the part that fails silently, and a second copy of it
        would eventually disagree with this one about which routes are in
        milliseconds.
        """
        threshold = int(since * 1000) if millis else int(since)
        return _record_list(self._get(path, fields=fields, filter=f"{time_field}:>{threshold}"))

    def missing_patches(self, pool_id: str) -> list[dict[str, object]]:
        """Patches Xen Orchestra reports as missing on one pool.

        Per-pool rather than global because that is the route XO offers. A 403
        here is not fatal to a findings run: on XOA the patch list needs a
        support subscription, so the caller treats a refusal as "not available"
        rather than failing the whole report.
        """
        response = self._get(MISSING_PATCHES_PATH.format(pool_id=pool_id))
        if response.status_code in (401, 403, 404):
            raise XoError(
                f"Xen Orchestra would not report missing patches for pool {pool_id} "
                f"(HTTP {response.status_code}). On XOA this needs a support "
                f"subscription; the rest of the findings are unaffected."
            )
        return _record_list(response)

    def pool_dashboard(self) -> dict[str, object]:
        """The pool dashboard totals: patches, backups, storage, host state.

        Administrator-only — it is the same route ``is_admin`` probes, for the
        same reason it can be used as that probe. A restricted account is
        refused outright rather than answered with an empty summary, so this
        raises and the caller records the source as unavailable.
        """
        response = self._get(DASHBOARD_PATH)
        if response.status_code in (401, 403):
            raise XoError(
                "Xen Orchestra refused the pool dashboard. It carries no ACL "
                "filtering, so it needs an administrator account."
            )
        if response.status_code != 200:
            raise XoError(
                f"Xen Orchestra returned HTTP {response.status_code} for {DASHBOARD_PATH}."
            )
        try:
            payload = response.json()
        except ValueError:
            raise XoError(f"Xen Orchestra did not return JSON for {DASHBOARD_PATH}.") from None
        if not isinstance(payload, dict):
            raise XoError(f"Xen Orchestra returned an unexpected shape for {DASHBOARD_PATH}.")
        return payload

    # -- privileges ------------------------------------------------------

    def is_admin(self) -> bool:
        """True when the account has XO administrator permission.

        There is no "who am I" route reporting the permission level, so this
        asks for a route that refuses non-administrators outright.

        The route has to be chosen carefully. Most collection routes are
        ACL-filtered and answer a restricted account with ``200 []`` rather
        than a refusal — measured: ``/acl-roles``, ``/acl-privileges`` and
        ``/users`` all do, so none of them can tell the two apart. ``/dashboard``
        carries no ACL middleware, which in XO means administrator-only, and
        was measured returning 200 for an admin and 403 for a restricted
        account on the same instance.
        """
        response = self._get(DASHBOARD_PATH)
        return response.status_code == 200

    def grantable_host_actions(self) -> set[str]:
        """Return the host actions this instance can grant to a role.

        The privilege catalogue is stored per instance and can be older than
        the API serving it, so what a role may be granted is a question about
        this deployment rather than about a version number.
        """
        response = self._get("/acl-privileges", fields="action,resource", limit=1000)
        if response.status_code != 200:
            return set()
        try:
            records = response.json()
        except ValueError:
            return set()
        return {
            record["action"]
            for record in records
            if isinstance(record, dict)
            and record.get("resource") == LOG_EXPORT_RESOURCE
            and "action" in record
        }

    def check_log_export(self, *, is_admin: bool) -> LogExportSupport:
        """Work out whether this connection will be able to download logs.

        An administrator always can. For anyone else the answer depends on the
        instance: XO requires ``host:export:logs`` for the log routes, but some
        instances carry a privilege catalogue seeded before that action
        existed, and cannot grant it to a role at all. Where that is so, the
        only privilege reaching the logs is ``host:*`` — full host
        administration, which is not a restricted account in any useful sense.
        """
        if is_admin:
            return LogExportSupport(True, "the account is an administrator")

        actions = self.grantable_host_actions()
        if not actions:
            return LogExportSupport(
                False,
                "the account cannot read this instance's privilege catalogue, "
                "so log access could not be determined",
            )

        if LOG_EXPORT_ACTION in actions:
            return LogExportSupport(
                False,
                f"the account needs the host {LOG_EXPORT_ACTION!r} privilege, "
                f"which this instance can grant",
            )

        detail = (
            f"this Xen Orchestra cannot grant host {LOG_EXPORT_ACTION!r} to a role — "
            f"its privilege catalogue offers only {', '.join(sorted(actions))}. "
            f"Log collection needs an administrator account here"
        )
        return LogExportSupport(False, detail, grantable=False)

    # -- log download ----------------------------------------------------

    def download_to(
        self,
        path: str,
        destination,
        *,
        on_chunk=None,
    ) -> int:
        """Stream one XO route to a file. Returns the bytes written.

        Deliberately not ``_get``: that reads the whole body into the response
        object, which for a 433 MB bundle means holding it in memory before it
        ever reaches the disk. This streams it, so the peak cost is one chunk.
        Do not add a third GET — extend one of these two.

        ``on_chunk(written, total)`` is called after each chunk, with ``total``
        from Content-Length or ``None`` when the host does not send one. It is
        how a caller reports progress, and — because a job body raises from it
        to cancel — how a download is interrupted without the transfer needing
        to know what a job is. The partial file is removed on the way out.

        A refusal is an XoError naming the privilege, because the operator's
        next move is to change the account, not to retry.
        """
        try:
            with self._client_for_download() as client:
                with client.stream("GET", path) as response:
                    if response.status_code in (401, 403):
                        # The body carries the missing privileges, but it has
                        # not been read at this point in a streamed response.
                        response.read()
                        raise XoError(
                            f"Xen Orchestra refused {path}. Downloading logs needs the "
                            f"host {LOG_EXPORT_ACTION!r} privilege, which in practice "
                            f"means an administrator account on most instances."
                        )
                    if response.status_code == 404:
                        raise XoError(f"Xen Orchestra has no {path}. Check the host still exists.")
                    if response.status_code != 200:
                        raise XoError(
                            f"Xen Orchestra returned HTTP {response.status_code} for {path}."
                        )

                    total = _content_length(response)
                    written = 0
                    tail = b""
                    with destination.open("wb") as handle:
                        try:
                            for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                                handle.write(chunk)
                                written += len(chunk)
                                tail = (tail + chunk)[-_TAIL_BYTES:]
                                if on_chunk is not None:
                                    on_chunk(written, total)
                        except httpx.ReadTimeout:
                            # A read timeout with data already received is the
                            # expected ending here, not a failure.
                            #
                            # Measured against one pool through nginx: every
                            # byte of the bundle arrives and the response then
                            # never terminates, so the read blocks until the
                            # timeout with the complete file already on disk.
                            # Treating that as a failure threw away a finished
                            # download; what arrived is kept and validated the
                            # same way a cleanly ended transfer is.
                            #
                            # With nothing received it is a real stall, and the
                            # handler below reports it.
                            if not written:
                                raise
                        except (
                            httpx.RemoteProtocolError,
                            httpx.StreamClosed,
                            httpx.ReadError,
                        ) as exc:
                            # A transfer genuinely cut short. Everything
                            # received is real data, and reporting the
                            # truncation beats a bare protocol error that reads
                            # as a network fault and sends the operator looking
                            # in the wrong place.
                            #
                            # ReadError is here because a connection reset
                            # arrives as one, not as a protocol error: measured
                            # against a socket closed with SO_LINGER 0, httpx
                            # raises ReadError("[Errno 104] Connection reset by
                            # peer"). Without it a reset fell through to the
                            # generic handler, which deletes the file — so the
                            # one case where the bytes are most expensive to
                            # fetch again was the one that discarded them.
                            raise XoError(
                                f"The transfer ended after {written / 1024 / 1024:.0f} MiB "
                                f"without finishing. {_TRUNCATED_HINT}"
                            ) from exc

                    # The host may append an error page after the archive
                    # bytes; drop it so what is left is the archive alone.
                    written -= _trim_host_error_page(destination, tail)
                    return written
        except httpx.TimeoutException as exc:
            destination.unlink(missing_ok=True)
            raise XoError(
                f"{self.url} stopped sending data for {DOWNLOAD_TIMEOUT:.0f}s during {path}. "
                f"A log download cannot resume — it has to be started again."
            ) from exc
        except httpx.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise XoError(f"download of {path} from {self.url} failed: {exc}") from exc
        except BaseException:
            # Cancellation raises through on_chunk. A half-written 433 MB file
            # left on the data volume is the worst possible remnant.
            destination.unlink(missing_ok=True)
            raise

    def _client_for_download(self) -> httpx.Client:
        """A client with the download timeout rather than the request one.

        Same construction as ``_client`` otherwise, and built from it, so
        authentication and TLS handling stay defined in exactly one place.
        """
        client = self._client()
        client.timeout = httpx.Timeout(DOWNLOAD_TIMEOUT)
        return client

    def download_logs(self, host_id: str, destination, *, on_chunk=None) -> int:
        """Stream a host's log bundle to ``destination``. Returns bytes written."""
        return self.download_to(LOGS_PATH.format(host_id=host_id), destination, on_chunk=on_chunk)

    def download_audit(self, host_id: str, destination, *, on_chunk=None) -> int:
        """Stream a host's XAPI audit trail to ``destination``."""
        return self.download_to(AUDIT_PATH.format(host_id=host_id), destination, on_chunk=on_chunk)

    # -- connection test -------------------------------------------------

    def test_connection(self) -> ConnectionTest:
        """Check the URL and token, and report what this account can reach.

        Deliberately more than a reachability probe. A restricted account gets
        HTTP 200 and an empty array from the collection routes rather than a
        403, so "the request succeeded" is not evidence the connection is
        usable — an account that can see nothing looks exactly like a healthy
        connection to an empty pool.
        """
        response = self._get("/pools")

        if response.status_code in (401, 403):
            return ConnectionTest(
                False,
                "Xen Orchestra rejected the token. Check it has not been "
                "revoked, and that it belongs to the account you expect.",
            )
        if response.status_code == 404:
            return ConnectionTest(
                False,
                f"No REST API at {self.url}{API_BASE}. Check the address points "
                f"at Xen Orchestra itself.",
            )
        if response.status_code != 200:
            return ConnectionTest(
                False,
                f"Xen Orchestra returned HTTP {response.status_code} for {API_BASE}/pools.",
            )

        try:
            pools = response.json()
        except ValueError:
            return ConnectionTest(
                False,
                f"{self.url} answered but did not return JSON. Check the address "
                f"points at Xen Orchestra rather than a proxy or login page.",
            )

        hosts = self.list_hosts()
        is_admin = self.is_admin()
        log_export = self.check_log_export(is_admin=is_admin)

        warnings: list[str] = []
        if not pools:
            warnings.append(
                "This account can see no pools. Xen Orchestra returns an empty "
                "list rather than an error when an account lacks read access, "
                "so this usually means missing privileges rather than an empty "
                "installation."
            )
        if not log_export.available:
            warnings.append(f"Log collection: {log_export.summary}.")

        account = "an administrator" if is_admin else "a restricted account"
        return ConnectionTest(
            ok=True,
            message=(
                f"Connected to {self.url} as {account}. "
                f"Visible: {len(pools)} pool(s), {len(hosts)} host(s)."
            ),
            is_admin=is_admin,
            pool_count=len(pools),
            host_count=len(hosts),
            log_export=log_export,
            warnings=warnings,
        )


def _raise_for_collection(response: httpx.Response) -> None:
    """Raise XoError unless this collection response is a usable 200.

    Deliberately shared by both readers below, because the distinction they
    depend on is the same one and must not drift: ``200 []`` is a real answer
    meaning the account sees nothing, while any other status is a failure that
    has to travel rather than be flattened into an empty collection.

    Flattening is what makes an outage indistinguishable from a privilege
    problem: a refused, rate-limited or proxied-away request would otherwise
    produce an empty inventory, be stored as a successful result, and be shown
    to the operator as "this account can see nothing".
    """
    if response.status_code == 200:
        return
    if response.status_code in (401, 403):
        raise XoError(
            "Xen Orchestra refused the token when reading "
            f"{response.request.url.path}. Check it has not been revoked."
        )
    raise XoError(
        f"Xen Orchestra returned HTTP {response.status_code} for {response.request.url.path}."
    )


# How much of the end of a download to keep for inspection. XCP-ng appends its
# error page after the archive bytes, so only the tail has to be examined — and
# a fixed window means a 433 MB body is still never held.
_TAIL_BYTES = 4096

# Said whenever a bundle arrives truncated. The cause is on the host rather
# than in Xen Orchestra or here, so the operator is pointed at the right place.
_TRUNCATED_HINT = (
    "The cause is upstream of XCP Pulse — usually a reverse proxy in front of Xen Orchestra."
)


def _trim_host_error_page(destination, tail: bytes) -> int:
    """Cut an appended error page off a download. Returns bytes removed.

    Measured on XCP-ng 8.3: a bundle build that fails part-way ends with a few
    hundred bytes of HTML after the archive data, and the request still
    finishes with HTTP 200 — so the status says success while the file has
    rubbish on the end.

    Trimming rather than refusing, because the archive before that point is
    real log data that the repack can still read, and a collection costs
    minutes to repeat. Throwing away 450 MB of readable logs over 263 bytes of
    trailing HTML is the wrong trade — the job reports that the bundle ends
    early, which is what the operator needs to know before sending it on.
    """
    marker = tail.lower().find(b"<html")
    if marker == -1:
        return 0

    # The page sits at the very end, so its length within the tail window is
    # how much to drop from the file.
    removed = len(tail) - marker
    size = destination.stat().st_size
    if removed >= size:
        return 0
    with destination.open("r+b") as handle:
        handle.truncate(size - removed)
    return removed


def _content_length(response: httpx.Response) -> int | None:
    """The declared body size, or None when the host does not say.

    XO sends no Content-Length for ``logs.tgz`` on some versions, which is why
    every caller has to cope with not knowing the total — a progress bar that
    needs one would be a progress bar that sometimes cannot be drawn.
    """
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _href_list(response: httpx.Response) -> list[str]:
    """Read a collection response into a list of hrefs.

    XO returns collections as an array of href strings by default. A restricted
    account gets 200 and an empty array, which is a real answer and not an
    error; anything else raises.
    """
    _raise_for_collection(response)
    try:
        payload = response.json()
    except ValueError:
        raise XoError(
            f"Xen Orchestra did not return JSON for {response.request.url.path}."
        ) from None
    if not isinstance(payload, list):
        raise XoError(
            f"Xen Orchestra returned an unexpected shape for {response.request.url.path}."
        )
    return [item for item in payload if isinstance(item, str)]


def _record_list(response: httpx.Response) -> list[dict[str, object]]:
    """Read a collection response into a list of objects.

    Mirrors _href_list for the ``fields`` form of the same routes, and fails
    the same way: only ``200 []`` is an empty inventory.
    """
    _raise_for_collection(response)
    try:
        payload = response.json()
    except ValueError:
        raise XoError(
            f"Xen Orchestra did not return JSON for {response.request.url.path}."
        ) from None
    if not isinstance(payload, list):
        raise XoError(
            f"Xen Orchestra returned an unexpected shape for {response.request.url.path}."
        )
    return [item for item in payload if isinstance(item, dict)]


def _label(record: dict[str, object], kind: str) -> str:
    """The display name for a record, falling back to its id.

    A pool or host may legitimately have an empty name_label in XO, and an
    unnamed row is far more confusing than one showing its uuid.
    """
    name = str(record.get("name_label") or "").strip()
    if name:
        return name
    identifier = str(record.get("id") or "").strip()
    return identifier or f"unnamed {kind}"


def _nested_int(record: dict[str, object], outer: str, inner: str) -> int:
    """Read record[outer][inner] as an int, or 0 when absent or malformed."""
    container = record.get(outer)
    if not isinstance(container, dict):
        return 0
    value = container.get(inner)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
