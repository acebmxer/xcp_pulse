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

# Granting every action on hosts implies log export. It is full host
# administration, so it is reported as such rather than as a way to stay
# restricted.
WILDCARD_ACTION = "*"

# Administrator-only because it carries no ACL middleware. Used to tell an
# administrator from a restricted account: routes that are ACL-filtered answer
# both with 200 and cannot distinguish them.
DASHBOARD_PATH = "/dashboard"

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


def _href_list(response: httpx.Response) -> list[str]:
    """Read a collection response into a list of hrefs.

    XO returns collections as an array of href strings by default. A restricted
    account gets 200 and an empty array, which is a real answer and not an
    error, so a non-200 is the only thing treated as failure.
    """
    if response.status_code != 200:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, str)]
