"""The Xen Orchestra client, against a stubbed API.

The responses here are shaped from real ones observed against a live XO CE
instance, so these lock in behaviours that are easy to get wrong from the
documentation alone — in particular that a restricted account is answered with
200 and an empty list, not a 403.
"""

from __future__ import annotations

import httpx
import pytest

from app.xo_client import XoClient, XoError

POOL_HREF = "/rest/v0/pools/751d40fa-60b5-82cf-e735-6ed42d0e03f8"
HOST_HREF = "/rest/v0/hosts/c1372ec6-3651-4481-808b-34293e53e144"


def _client(handler: object, **kwargs: object) -> XoClient:
    """An XoClient whose HTTP calls are served by ``handler``."""
    client = XoClient("https://xo.example.com", "token", **kwargs)  # type: ignore[arg-type]
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    original = client._client

    def patched() -> httpx.Client:
        real = original()
        return httpx.Client(
            base_url=real.base_url,
            cookies=real.cookies,
            timeout=real.timeout,
            transport=transport,
        )

    client._client = patched  # type: ignore[method-assign]
    return client


def _privileges(actions: list[str]) -> list[dict[str, str]]:
    return [{"action": action, "resource": "host"} for action in actions]


def test_admin_connection_reports_what_it_can_see() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=[POOL_HREF])
        if request.url.path.endswith("/hosts"):
            return httpx.Response(200, json=[HOST_HREF, HOST_HREF + "b"])
        if request.url.path.endswith("/dashboard"):
            return httpx.Response(200, json={"nPools": 1})
        return httpx.Response(200, json=[])

    result = _client(handler).test_connection()
    assert result.ok is True
    assert result.is_admin is True
    assert result.pool_count == 1
    assert result.host_count == 2
    assert result.log_export is not None
    assert result.log_export.available is True


def test_admin_detection_ignores_acl_filtered_routes() -> None:
    """A restricted account must not be reported as an administrator.

    Regression: admin was originally detected by whether /acl-roles returned
    200. Measured against a live instance, a restricted account gets 200 and an
    empty array there — as it does from /acl-privileges and /users — so the
    check reported every account as an administrator, and in turn claimed log
    export was available to all of them. Only /dashboard, which carries no ACL
    middleware, actually refuses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dashboard"):
            return httpx.Response(403, json={"error": "unauthorized"})
        # Everything ACL-filtered answers 200 with nothing in it.
        return httpx.Response(200, json=[])

    client = _client(handler)
    assert client.is_admin() is False
    assert client.test_connection().is_admin is False


def test_admin_detection_accepts_the_dashboard_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dashboard"):
            return httpx.Response(200, json={"nPools": 1})
        return httpx.Response(200, json=[])

    assert _client(handler).is_admin() is True


def test_restricted_account_gets_200_and_an_empty_list() -> None:
    """Measured behaviour: no 403 on collections, just nothing in them.

    A test that treated 200 as success would call this connection healthy.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dashboard"):
            return httpx.Response(403, json={"error": "unauthorized"})
        if request.url.path.endswith("/acl-privileges"):
            return httpx.Response(200, json=_privileges(["read", "allow-vm", "*"]))
        return httpx.Response(200, json=[])

    result = _client(handler).test_connection()
    assert result.ok is True
    assert result.is_admin is False
    assert result.pool_count == 0
    # The empty inventory must be called out rather than passing silently.
    assert any("no pools" in warning for warning in result.warnings)


def test_log_export_unavailable_when_the_catalogue_cannot_grant_it() -> None:
    """The measured case: host actions are read/allow-vm/* and nothing else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dashboard"):
            return httpx.Response(403, json={"error": "unauthorized"})
        if request.url.path.endswith("/acl-privileges"):
            return httpx.Response(200, json=_privileges(["read", "allow-vm", "*"]))
        return httpx.Response(200, json=[])

    support = _client(handler).check_log_export(is_admin=False)
    assert support.available is False
    assert support.grantable is False
    assert "administrator" in support.reason


def test_log_export_grantable_when_the_catalogue_offers_it() -> None:
    """An instance whose catalogue includes export:logs is a different answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acl-privileges"):
            return httpx.Response(200, json=_privileges(["read", "export:logs", "*"]))
        return httpx.Response(200, json=[])

    support = _client(handler).check_log_export(is_admin=False)
    assert support.available is False
    assert support.grantable is True


def test_admin_always_has_log_export() -> None:
    support = _client(lambda r: httpx.Response(200, json=[])).check_log_export(is_admin=True)
    assert support.available is True


def test_bad_token_is_reported_as_a_rejected_token() -> None:
    result = _client(lambda r: httpx.Response(401, json={"error": "invalid"})).test_connection()
    assert result.ok is False
    assert "token" in result.message.lower()


def test_wrong_address_is_reported_as_no_rest_api() -> None:
    result = _client(lambda r: httpx.Response(404, text="Not Found")).test_connection()
    assert result.ok is False
    assert "REST API" in result.message


def test_non_json_response_is_reported_clearly() -> None:
    """Pointing at a proxy or login page must not raise a parse error."""
    result = _client(lambda r: httpx.Response(200, text="<html>login</html>")).test_connection()
    assert result.ok is False
    assert "did not return JSON" in result.message


def test_unreachable_host_raises_xo_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(XoError) as excinfo:
        _client(handler).test_connection()
    assert "cannot reach" in str(excinfo.value)


def test_timeout_raises_xo_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(XoError) as excinfo:
        _client(handler).test_connection()
    assert "timed out" in str(excinfo.value)


def test_list_hosts_returns_hrefs() -> None:
    client = _client(lambda r: httpx.Response(200, json=[HOST_HREF]))
    assert client.list_hosts() == [HOST_HREF]


# -- inventory -----------------------------------------------------------
#
# The records below are trimmed copies of real responses from a live XO CE
# instance (@xen-orchestra/rest-api 0.39.0), so the field names and nesting
# here are the ones actually served, not ones inferred from documentation.

POOL_ID = "751d40fa-60b5-82cf-e735-6ed42d0e03f8"
MASTER_ID = "c1372ec6-3651-4481-808b-34293e53e144"
SECOND_HOST_ID = "35233210-4e37-4703-9bf6-9e8a9c24df9f"

POOL_RECORD = {"id": POOL_ID, "name_label": "xcp-ng-Pool1", "master": MASTER_ID}

HOST_RECORDS = [
    {
        "id": SECOND_HOST_ID,
        "name_label": "xcp-ng-host2",
        "address": "10.100.2.11",
        "version": "8.3.0",
        "productBrand": "XCP-ng",
        "power_state": "Running",
        "enabled": True,
        "$pool": POOL_ID,
        "cpus": {"cores": 24, "sockets": 1},
        "memory": {"usage": 32056504320, "size": 103079215104},
    },
    {
        "id": MASTER_ID,
        "name_label": "xcp-ng-host1",
        "address": "10.100.2.10",
        "version": "8.3.0",
        "productBrand": "XCP-ng",
        "power_state": "Running",
        "enabled": True,
        "$pool": POOL_ID,
        "cpus": {"cores": 28, "sockets": 1},
        "memory": {"usage": 38540980224, "size": 103079215104},
    },
]


def _inventory_handler(
    pools: list[dict[str, object]],
    hosts: list[dict[str, object]],
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=pools)
        if request.url.path.endswith("/hosts"):
            return httpx.Response(200, json=hosts)
        return httpx.Response(404)

    return handler


def test_inventory_reads_pools_and_hosts() -> None:
    inventory = _client(_inventory_handler([POOL_RECORD], HOST_RECORDS)).inventory()

    assert [pool.name for pool in inventory.pools] == ["xcp-ng-Pool1"]
    assert len(inventory.hosts) == 2
    assert inventory.is_empty is False

    host = next(host for host in inventory.hosts if host.id == MASTER_ID)
    assert host.name == "xcp-ng-host1"
    assert host.address == "10.100.2.10"
    assert host.product == "XCP-ng"
    assert host.version == "8.3.0"
    assert host.cpu_cores == 28
    assert host.running is True
    assert host.memory_percent == 37


def test_inventory_requests_fields_so_records_are_objects() -> None:
    """Without ``fields`` XO answers with href strings, which cannot be shown.

    Regression guard: the count-only helpers deliberately omit ``fields``, so
    it would be easy for the inventory to lose it and silently render nothing.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    _client(handler).inventory()
    assert all("fields=" in url for url in seen), seen


def test_inventory_groups_hosts_under_their_pool() -> None:
    inventory = _client(_inventory_handler([POOL_RECORD], HOST_RECORDS)).inventory()

    names = [host.name for host in inventory.hosts_in(POOL_ID)]
    assert names == ["xcp-ng-host1", "xcp-ng-host2"], "hosts should be sorted by name"
    assert inventory.orphan_hosts == []


def test_inventory_keeps_hosts_whose_pool_is_not_visible() -> None:
    """Pool and host read privileges are granted separately in XO.

    An account can be allowed a host while refused the pool containing it, and
    dropping that host would under-report what XCP Pulse can reach.
    """
    inventory = _client(_inventory_handler([], HOST_RECORDS)).inventory()

    assert inventory.pools == []
    assert [host.name for host in inventory.orphan_hosts] == [
        "xcp-ng-host1",
        "xcp-ng-host2",
    ]


def test_inventory_is_empty_for_an_account_without_privileges() -> None:
    """Measured: a restricted account gets 200 and [], not 403."""
    inventory = _client(_inventory_handler([], [])).inventory()

    assert inventory.is_empty is True
    assert inventory.pools == []
    assert inventory.hosts == []


def test_inventory_tolerates_missing_and_malformed_fields() -> None:
    """An older instance may not serve every field asked for.

    Absent fields must render as unknown rather than raising, because a single
    unusual host would otherwise take out the whole dashboard.
    """
    records = [
        {"id": "bare-host", "$pool": POOL_ID},
        {"id": "odd-host", "name_label": "", "memory": "not-a-dict", "cpus": None},
    ]
    inventory = _client(_inventory_handler([{"id": POOL_ID}], records)).inventory()

    bare = inventory.hosts[0]
    assert bare.name == "bare-host", "an unnamed host falls back to its id"
    assert bare.memory_percent is None
    assert bare.address == ""

    odd = inventory.hosts[1]
    assert odd.name == "odd-host"
    assert odd.cpu_cores == 0
    assert inventory.pools[0].name == POOL_ID


def test_inventory_surfaces_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(XoError):
        _client(handler).inventory()
