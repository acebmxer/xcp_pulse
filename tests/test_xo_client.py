"""The Xen Orchestra client, against a stubbed API.

The responses here are shaped from real ones observed against a live XO CE
instance, so these lock in behaviours that are easy to get wrong from the
documentation alone — in particular that a restricted account is answered with
200 and an empty list, not a 403.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.xo_client import XoClient, XoError, _content_length

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


def test_inventory_raises_when_pools_are_refused() -> None:
    """A refusal must not be reported as an inventory with nothing in it.

    Regression, measured on a live instance: the collection readers turned any
    non-200 into an empty list, so a refused or failing read produced an empty
    Inventory, was stored as a *successful* refresh, and was shown to the
    operator as "this account can see no pools or hosts" — a privilege claim
    the code had never established. Only 200 [] means empty.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(XoError, match="refused the token"):
        _client(handler).inventory()


def test_inventory_raises_when_hosts_fail_after_pools_succeed() -> None:
    """A failure on the second call must not silently yield pools and no hosts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pools"):
            return httpx.Response(200, json=[{"id": POOL_ID, "name_label": "Pool1"}])
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(XoError, match="HTTP 502"):
        _client(handler).inventory()


def test_inventory_raises_when_the_response_is_not_json() -> None:
    """A proxy login page answering 200 must not read as an empty inventory."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(XoError, match="did not return JSON"):
        _client(handler).inventory()


def test_list_hosts_raises_on_a_server_error() -> None:
    """The href reader fails the same way as the record reader."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(XoError, match="HTTP 500"):
        _client(handler).list_hosts()


# ---- streaming downloads -------------------------------------------------
#
# The log bundle is measured at 433 MB, so what matters here is that the body
# reaches the disk without being held, that a refusal names the privilege the
# operator has to change, and that a failure never leaves a partial file behind
# for someone to mistake for a collection.


def _download_client(handler: object) -> XoClient:
    """Like ``_client``, but with the download timeout the real one uses.

    Built through ``_client`` so the download path under test is the one the
    application uses, rather than a second construction that could drift.
    """
    return _client(handler)


def test_a_download_streams_the_body_to_the_named_file(tmp_path) -> None:
    payload = b"x" * (3 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/hosts/host-1/logs.tgz")
        return httpx.Response(200, content=payload)

    destination = tmp_path / "bundle.tgz"
    written = _download_client(handler).download_logs("host-1", destination)

    assert written == len(payload)
    assert destination.read_bytes() == payload


def test_a_download_reports_progress_with_the_declared_total(tmp_path) -> None:
    """The total is what the ETA on the collect page is computed from."""
    payload = b"y" * (2 * 1024 * 1024 + 17)
    seen: list[tuple[int, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    _download_client(handler).download_logs(
        "host-1",
        tmp_path / "bundle.tgz",
        on_chunk=lambda written, total: seen.append((written, total)),
    )

    assert seen[-1] == (len(payload), len(payload))
    assert all(total == len(payload) for _, total in seen)


def test_a_response_without_a_content_length_has_no_total_to_report() -> None:
    """Some XO versions send none, so a progress bar cannot assume one.

    Checked against ``_content_length`` rather than through a download: httpx's
    MockTransport sets a Content-Length on every response it builds, so a
    transport-level test cannot produce a reply that lacks one.
    """
    assert _content_length(httpx.Response(200)) is None
    assert _content_length(httpx.Response(200, headers={"content-length": "0"})) is None
    assert _content_length(httpx.Response(200, headers={"content-length": "nonsense"})) is None
    assert _content_length(httpx.Response(200, headers={"content-length": "433"})) == 433


def test_a_download_whose_total_is_unknown_still_reports_what_has_arrived(
    tmp_path,
) -> None:
    """The progress line falls back to bytes-so-far rather than a percentage."""
    seen: list[tuple[int, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"z" * 1024)

    client = _download_client(handler)
    with patch("app.xo_client._content_length", return_value=None):
        client.download_audit(
            "host-1",
            tmp_path / "audit.txt",
            on_chunk=lambda written, total: seen.append((written, total)),
        )

    assert seen and all(total is None for _, total in seen)


def test_a_refused_download_names_the_privilege_that_is_missing(tmp_path) -> None:
    """A restricted account is the expected failure, so it must read clearly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"data": {"missingPrivileges": ["host:export:logs"]}})

    destination = tmp_path / "bundle.tgz"
    with pytest.raises(XoError) as excinfo:
        _download_client(handler).download_logs("host-1", destination)

    assert "export:logs" in str(excinfo.value)
    assert not destination.exists()


def test_a_download_of_a_host_that_is_gone_says_so(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(XoError) as excinfo:
        _download_client(handler).download_logs("host-1", tmp_path / "bundle.tgz")

    assert "no" in str(excinfo.value).lower()


def test_a_failure_mid_transfer_leaves_no_partial_file(tmp_path) -> None:
    """A half-written 433 MB file is the worst possible remnant."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped")

    destination = tmp_path / "bundle.tgz"
    with pytest.raises(XoError):
        _download_client(handler).download_logs("host-1", destination)

    assert not destination.exists()


def test_cancelling_from_the_progress_callback_removes_the_partial_file(tmp_path) -> None:
    """How a job cancels a download: the callback raises, and nothing is left."""

    class _Cancelled(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"a" * (4 * 1024 * 1024))

    def cancel(written: int, total: int | None) -> None:
        raise _Cancelled

    destination = tmp_path / "bundle.tgz"
    with pytest.raises(_Cancelled):
        _download_client(handler).download_logs("host-1", destination, on_chunk=cancel)

    assert not destination.exists()


def test_an_unexpected_status_is_reported_with_its_code(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    with pytest.raises(XoError) as excinfo:
        _download_client(handler).download_logs("host-1", tmp_path / "bundle.tgz")

    assert "502" in str(excinfo.value)


def test_a_truncated_transfer_says_what_happened_and_keeps_nothing(
    tmp_path,
) -> None:
    """Measured on XCP-ng 8.3: the host's bundle process dies part-way.

    Xen Orchestra streams what it has and the connection is reset without a
    terminating chunk. A bare protocol error here reads as a network fault and
    sends the operator looking in the wrong place.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        def body():
            yield b"x" * 1024
            raise httpx.RemoteProtocolError("incomplete chunked read")

        return httpx.Response(200, content=body())

    destination = tmp_path / "bundle.tgz"
    with pytest.raises(XoError) as excinfo:
        _download_client(handler).download_logs("host-1", destination)

    message = str(excinfo.value)
    assert "without finishing" in message
    assert "upstream of XCP Pulse" in message
    assert not destination.exists()


def test_an_error_page_appended_to_the_archive_is_trimmed_off(tmp_path) -> None:
    """The request finishes 200 while the file has rubbish on the end.

    XCP-ng appends a few hundred bytes of HTML after the archive data when its
    bundle build fails part-way. Trimming rather than refusing, because what
    comes before is real log data the repack can still read, and a collection
    costs minutes to repeat.
    """
    archive = b"\x1f\x8b" + b"g" * 2048
    page = (
        b"<html><body>An error occurred; please wait a while and try again."
        b"<h1> Additional information </h1>"
        b"Forkhelpers.Subprocess_failed(1)</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=archive + page)

    destination = tmp_path / "bundle.tgz"
    written = _download_client(handler).download_logs("host-1", destination)

    assert destination.read_bytes() == archive, "the page must be cut off exactly"
    assert written == len(archive)
    assert b"<html" not in destination.read_bytes()


def test_a_clean_download_is_not_mistaken_for_an_error_page(tmp_path) -> None:
    """The tail scan must not fire on archive bytes that merely look textual."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x1f\x8b" + b"<not html>" * 100)

    destination = tmp_path / "bundle.tgz"
    written = _download_client(handler).download_logs("host-1", destination)

    assert written == destination.stat().st_size


def test_a_read_timeout_with_data_already_received_ends_the_transfer(tmp_path) -> None:
    """Every byte arrives and the response never terminates.

    Measured against one pool through nginx: the complete file sat on disk
    while the read blocked for the whole timeout and the job then failed. With
    this handling the same download returns in 71 seconds with its bytes kept.

    Checked by driving the transfer to completion and then raising the timeout
    the way the transport does, because MockTransport cannot deliver a chunk
    and then time out — and a fake that timed out first would exercise the
    empty-stall path instead, which is the opposite case.
    """
    chunks = [b"\x1f\x8b" + b"a" * 4096]
    real_iter_bytes = httpx.Response.iter_bytes

    def fake_iter(self, chunk_size=None):
        # Only the streamed download is replaced. MockTransport builds its
        # response by reading the body first, so patching unconditionally
        # raises during construction and exercises the empty-stall path —
        # the opposite of what is under test here.
        if getattr(self, "_replace_stream", False):
            yield from chunks
            raise httpx.ReadTimeout("no terminator is coming")
        yield from real_iter_bytes(self, chunk_size)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ignored")

    original_stream = httpx.Client.stream

    def marking_stream(self, *args, **kwargs):
        context = original_stream(self, *args, **kwargs)

        class _Marked:
            def __enter__(inner):
                response = context.__enter__()
                response._replace_stream = True
                return response

            def __exit__(inner, *exc):
                return context.__exit__(*exc)

        return _Marked()

    destination = tmp_path / "bundle.tgz"
    with (
        patch.object(httpx.Response, "iter_bytes", fake_iter),
        patch.object(httpx.Client, "stream", marking_stream),
    ):
        written = _download_client(handler).download_logs("host-1", destination)

    assert written == 4098
    assert destination.exists(), "what arrived must be kept, not discarded"
    assert destination.read_bytes() == chunks[0]


def test_a_stall_with_nothing_received_is_still_a_failure(tmp_path) -> None:
    """A transfer that never starts is a real stall, not a finished download."""

    def fake_iter(self, chunk_size=None):
        raise httpx.ReadTimeout("nothing ever arrived")
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ignored")

    destination = tmp_path / "bundle.tgz"
    with patch.object(httpx.Response, "iter_bytes", fake_iter):
        with pytest.raises(XoError) as excinfo:
            _download_client(handler).download_logs("host-1", destination)

    assert "stopped sending data" in str(excinfo.value)
    assert not destination.exists()
